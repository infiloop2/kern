"""Dictation UI failure/recovery journeys with a deterministic microphone.

These exercise real browser storage and composer wiring. Speech recognition
is mocked; the offline engine is checked separately on a provisioned host.
"""

from playwright.sync_api import expect


MICROPHONE = """
(() => {
  window.voiceTracks = [];
  window.voiceDenied = false;
  // Override the prototype so WebKit wrapper collection cannot drop the mock.
  Object.defineProperty(MediaDevices.prototype, 'getUserMedia', {configurable: true, value: async () => {
    if (window.voiceDenied) throw new DOMException('denied', 'NotAllowedError');
    const track = {stopped: false, stop() {this.stopped = true;}, addEventListener(name, fn) {this[name] = fn;}};
    window.voiceTracks.push(track);
    const stream = {getTracks: () => [track]};
    if (window.voiceWait) {
      window.voiceWait = false;
      return new Promise(resolve => { window.resolveVoicePermission = () => resolve(stream); });
    }
    return stream;
  }});
  window.AudioContext = class {
    state = 'running';
    audioWorklet = {addModule: async () => {}};
    constructor() { window.voiceContext = this; }
    resume() {return Promise.resolve();}
    close() {this.state = 'closed'; return Promise.resolve();}
    createMediaStreamSource() {return {connect() {}};}
    addEventListener(name, fn) {this[name] = fn;}
  };
  window.AudioWorkletNode = class {
    constructor() {
      this.port = {onmessage: null, postMessage: () => queueMicrotask(() => this.port.onmessage({data: {stopped: true}}))};
      window.voiceNode = this;
    }
    connect() {}
    disconnect() {}
  };
  window.sayPhrase = () => {
    for (let i = 0; i < 20; i++) window.voiceNode.port.onmessage({data: {pcm: new Int16Array(1600).fill(i < 12 ? 4000 : 0)}});
  };
})();
"""


def run(page, url: str, log_in) -> None:
    page.add_init_script(MICROPHONE)
    ready = {"value": True}
    page.route("**/v1/dictation/ready", lambda route: route.fulfill(
        status=200 if ready["value"] else 503,
        json={"ready": True} if ready["value"] else {"error": {"message": "Transcription model isn't loaded yet. Click the mic to retry."}},
    ))
    responses = ["First spoken sentence.", "Second spoken sentence."]
    requests = []

    def transcribe(route):
        assert set(route.request.post_data_json) == {"audio"}
        requests.append(route.request)
        route.fulfill(json={"text": responses.pop(0)})

    page.route("**/v1/dictation/transcribe", transcribe)
    log_in(page, url)
    page.evaluate("location.hash = '#chat/new'")
    root = page.locator("#panel-workspace-chat")
    area = root.locator("#new-task")
    mic = root.locator(".dictation-mic")
    send = root.locator("#create-task")
    expect(area).to_be_visible()
    area.fill("Typed introduction.")

    # An unloaded model fails before asking for the microphone.
    ready["value"] = False
    mic.click()
    expect(mic).to_have_attribute("data-state", "warning")
    expect(root.locator(".dictation-status")).to_contain_text("model isn't loaded")
    assert page.evaluate("voiceTracks.length") == 0
    expect(send).to_be_enabled()
    ready["value"] = True

    # Cancel an outstanding permission request without holding Send hostage.
    page.evaluate("window.voiceWait = true")
    mic.click()
    page.wait_for_function("() => typeof window.resolveVoicePermission === 'function'")
    root.get_by_role("button", name="Stop dictation").click()
    expect(send).to_be_enabled()
    mic.click()
    expect(mic).to_have_attribute("data-state", "recording")
    page.evaluate("resolveVoicePermission()")
    page.wait_for_function("() => voiceTracks[0].stopped && !voiceTracks[1].stopped")
    expect(send).to_be_disabled()
    page.evaluate("sayPhrase()")
    expect(area).to_have_value("Typed introduction. First spoken sentence.")
    area.fill("Edited introduction. First spoken sentence.")
    area.evaluate("el => el.setSelectionRange(6, 6)")
    page.evaluate("sayPhrase()")
    original = "Edited introduction. First spoken sentence. Second spoken sentence."
    expect(area).to_have_value(original)
    assert area.evaluate("el => el.selectionStart") == 6
    root.get_by_role("button", name="Stop dictation").click()
    expect(send).to_be_enabled()
    expect(mic).to_have_attribute("data-state", "idle")

    # Done allows five seconds, then fences the draft. A late result is cached
    # for explicit Retry rather than appearing unexpectedly or being wasted.
    page.unroute("**/v1/dictation/transcribe")
    held = []
    page.route("**/v1/dictation/transcribe", lambda route: held.append(route))
    mic.click()
    expect(mic).to_have_attribute("data-state", "recording")
    page.evaluate("sayPhrase()")
    root.get_by_role("button", name="Stop dictation").click()
    expect(mic).to_have_attribute("data-state", "processing")
    expect(root.locator(".dictation-status")).to_be_hidden()
    expect(mic).to_have_attribute("data-state", "warning", timeout=7000)
    expect(send).to_be_disabled()
    assert len(held) == 1
    held.pop().fulfill(json={"text": "Late sentence."})
    page.wait_for_function("""async () => {
      const db = await new Promise(resolve => { const r = indexedDB.open('kern-dictation', 1); r.onsuccess = () => resolve(r.result); });
      const items = await new Promise(resolve => { const r = db.transaction('audio').objectStore('audio').getAll(); r.onsuccess = () => resolve(r.result); });
      db.close(); return items.some(item => item.text === 'Late sentence.');
    }""")
    expect(area).to_have_value(original)
    area.fill(original + " Typed while waiting.")
    mic.click()
    original += " Typed while waiting. Late sentence."
    expect(area).to_have_value(original)
    expect(send).to_be_enabled()
    assert len(held) == 0  # Cached result: no second host call.

    # A failed request survives reload, but nothing resumes automatically.
    page.unroute("**/v1/dictation/transcribe")
    page.route("**/v1/dictation/transcribe", lambda route: route.fulfill(status=503, json={"error": {"message": "Transcription is busy. Click the mic to retry."}}))
    mic.click()
    expect(mic).to_have_attribute("data-state", "recording")
    page.evaluate("sayPhrase()")
    expect(mic).to_have_attribute("data-state", "warning")
    page.reload()
    expect(area).to_have_value(original)
    expect(mic).to_have_attribute("aria-label", "Retry transcription")
    page.unroute("**/v1/dictation/transcribe")
    page.route("**/v1/dictation/transcribe", lambda route: route.fulfill(json={"text": "Recovered sentence."}))
    expect(area).to_have_value(original)
    mic.click()
    original += " Recovered sentence."
    expect(area).to_have_value(original)
    expect(send).to_be_enabled()

    # If committing the draft fails, preserve audio and the operator's edits.
    page.unroute("**/v1/dictation/transcribe")
    page.route("**/v1/dictation/transcribe", lambda route: route.fulfill(json={"text": "Storage recovery."}))
    page.evaluate("""() => {
      const original = Storage.prototype.setItem;
      window.failVoiceDraft = true;
      Storage.prototype.setItem = function(key, value) {
        if (window.failVoiceDraft && key === 'kern.agent-chat.composer-drafts.v1') throw new DOMException('quota', 'QuotaExceededError');
        return original.call(this, key, value);
      };
    }""")
    mic.click()
    expect(mic).to_have_attribute("data-state", "recording")
    page.evaluate("sayPhrase()")
    expect(mic).to_have_attribute("data-state", "warning")
    expect(area).to_have_value(original)
    page.evaluate("window.failVoiceDraft = false")
    mic.click()
    original += " Storage recovery."
    expect(area).to_have_value(original)
    expect(send).to_be_enabled()

    # A deletion failure after draft commit must remain idempotent after reload.
    page.evaluate("""() => {
      const original = IDBObjectStore.prototype.delete;
      IDBObjectStore.prototype.delete = function(key) {
        throw new DOMException('storage unavailable', 'UnknownError');
      };
    }""")
    mic.click()
    expect(mic).to_have_attribute("data-state", "recording")
    page.evaluate("sayPhrase()")
    original += " Storage recovery."
    expect(area).to_have_value(original)
    expect(mic).to_have_attribute("data-state", "warning")
    page.reload()
    expect(mic).to_have_attribute("aria-label", "Retry transcription")
    mic.click()
    expect(send).to_be_enabled()
    expect(area).to_have_value(original)

    # Navigation preserves the original recording without changing either
    # draft when its response arrives. Other conversations remain usable.
    page.unroute("**/v1/dictation/transcribe")
    page.route("**/v1/dictation/transcribe", lambda route: held.append(route))
    mic.click()
    expect(mic).to_have_attribute("data-state", "recording")
    page.evaluate("sayPhrase()")
    page.wait_for_timeout(100)
    page.evaluate("location.hash = '#chat/thread-1'")
    expect(area).not_to_have_value(original)
    page.wait_for_function("() => voiceTracks.every(track => track.stopped)")
    area.fill("Independent conversation.")
    expect(send).to_be_enabled()
    assert held
    held.pop().fulfill(json={"text": "Saved for the original conversation."})
    page.evaluate("location.hash = '#chat/new'")
    expect(area).to_have_value(original)
    expect(mic).to_have_attribute("aria-label", "Retry transcription")
    page.once("dialog", lambda dialog: dialog.accept())
    root.get_by_role("button", name="Discard pending recording").click()
    expect(send).to_be_enabled()
    expect(area).to_have_value(original)

    # Pause/Resume releases and reacquires the microphone; device interruptions
    # stop capture and keep ordinary text usable when no speech is pending.
    mic.click()
    expect(mic).to_have_attribute("data-state", "recording")
    root.get_by_role("button", name="Pause dictation", exact=True).last.click()
    expect(mic).to_have_attribute("aria-label", "Resume dictation")
    page.wait_for_function("() => voiceTracks.every(track => track.stopped)")
    mic.click()
    expect(mic).to_have_attribute("data-state", "recording")
    page.evaluate("voiceTracks[voiceTracks.length - 1].ended()")
    expect(mic).to_have_attribute("data-state", "warning")
    expect(root.locator(".dictation-status")).to_contain_text("disconnected")
    expect(send).to_be_enabled()
    mic.click()
    expect(mic).to_have_attribute("data-state", "recording")
    page.evaluate("voiceContext.state = 'suspended'; voiceContext.statechange()")
    expect(mic).to_have_attribute("data-state", "warning")
    expect(root.locator(".dictation-status")).to_contain_text("paused by your device")
    expect(send).to_be_enabled()

    # A malformed response preserves audio; explicit discard removes it.
    page.unroute("**/v1/dictation/transcribe")
    page.route("**/v1/dictation/transcribe", lambda route: route.fulfill(status=200, body="<html>bad gateway</html>"))
    mic.click()
    expect(mic).to_have_attribute("data-state", "recording")
    page.evaluate("sayPhrase()")
    expect(mic).to_have_attribute("data-state", "warning")
    expect(root.locator(".dictation-status")).to_contain_text("unreadable response")
    expect(area).to_have_value(original)
    page.once("dialog", lambda dialog: dialog.accept())
    root.get_by_role("button", name="Discard pending recording").click()
    expect(send).to_be_enabled()
    expect(area).to_have_value(original)

    # Microphone denial does not disable normal typing or sending.
    page.evaluate("window.voiceDenied = true")
    mic.click()
    expect(root.locator(".dictation-status")).to_contain_text("denied")
    expect(send).to_be_enabled()
    page.evaluate("window.voiceDenied = false")
    for width in (320, 390):
        page.set_viewport_size({"width": width, "height": 844})
        expect(mic).to_be_visible()
        bounds = mic.bounding_box()
        assert bounds and bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= width
    page.set_viewport_size({"width": 1280, "height": 900})

    # App Chat uses the same capture and recovery controls.
    page.unroute("**/v1/dictation/transcribe")
    page.route("**/v1/dictation/transcribe", lambda route: route.fulfill(json={"text": "App dictation."}))
    nav = page.get_by_role("button", name="Open navigation", exact=True)
    if nav.is_visible():
        nav.click()
    page.get_by_role("button", name="New app", exact=True).click()
    app = page.locator("#panel-workspace-web-apps")
    app.locator("#history-toggle").click()
    expect(app.locator("#message")).to_be_visible()
    app.locator(".dictation-mic").click()
    expect(app.locator(".dictation-mic")).to_have_attribute("data-state", "recording")
    page.evaluate("sayPhrase()")
    expect(app.locator("#message")).to_have_value("App dictation.")
    app.get_by_role("button", name="Stop dictation").click()
    expect(app.locator("#send-message")).to_be_enabled()

    # A crash after committing text but before deleting audio is idempotent.
    page.evaluate("""() => {
      const drafts = {}, textarea = document.createElement('textarea');
      const options = { drafts, storageKey: 'voice-receipt-test', key: 'conversation', currentKey: () => 'conversation', textarea, updated() {} };
      window.KernDictation.appendDraft(options, 'Once.', 'receipt');
      window.KernDictation.appendDraft(options, 'Once.', 'receipt');
      if (textarea.value !== 'Once.') throw new Error('Duplicate transcript');
      localStorage.removeItem(options.storageKey);
    }""")
