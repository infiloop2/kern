import fs from "node:fs";
import path from "node:path";
import readline from "node:readline";
import { pathToFileURL } from "node:url";

const stateDir = process.env.KERN_WHATSAPP_STATE_DIR || "/mnt/kern-admin/tools-state/whatsapp";
const moduleDir = process.env.KERN_HOST_NODE_MODULES || "/usr/local/lib/kern-node/node_modules";
const authDir = path.join(stateDir, "auth");
const storePath = path.join(stateDir, "messages.json");
const baileys = await import(pathToFileURL(path.join(moduleDir, "baileys", "lib", "index.js")));
const qrcodeModule = await import(pathToFileURL(path.join(moduleDir, "qrcode", "lib", "index.js")));
const qrcode = qrcodeModule.default || qrcodeModule;
const {
  default: makeWASocket,
  DisconnectReason,
  fetchLatestWaWebVersion,
  useMultiFileAuthState,
} = baileys;

const MAX_CHATS = 200;
const MAX_MESSAGES_PER_CHAT = 50;
const MAX_TEXT = 2000;
const LOGOUT_TIMEOUT_MS = 10000;
const VERSION_FETCH_TIMEOUT_MS = 5000;
const LINK_QR_TIMEOUT_MS = 30000;
const READABLE_CHAT_RE = /^[A-Za-z0-9._:-]{1,128}@(s\.whatsapp\.net|lid|g\.us)$/;
let socket = null;
let connectPromise = null;
let reconnectTimer = null;
let unbindSocketEvents = null;
let manualLogout = false;
let connectionGeneration = 0;
let qrGeneration = 0;
let status = "disconnected";
let account = null;
let qrDataUrl = "";
let lastError = "";
let connectionUpdateCount = 0;
let lastConnectionEvent = "none";
let lastDisconnectCode = null;
let lastFailure = { phase: "", error_name: "", error_code: "", status_code: null };
let transportPhase = "idle";
let linkTimedOut = false;
let saveTimer = null;
let credentialWriteChain = Promise.resolve();
const pendingCredentialWrites = new Set();
let cleanupPromise = null;

function diagnosticToken(value) {
  const token = String(value || "");
  return /^[A-Za-z0-9_.:-]{1,80}$/.test(token) ? token : "";
}

function safeFailure(error, phase) {
  const rawStatus = error?.output?.statusCode ?? error?.statusCode ?? error?.status;
  const statusCode = Number(rawStatus);
  return {
    phase,
    error_name: diagnosticToken(error?.name),
    error_code: diagnosticToken(error?.code ?? error?.cause?.code),
    status_code: Number.isInteger(statusCode) ? statusCode : null,
  };
}

function captureBaileysLog(level, args) {
  const message = [...args].reverse().find(value => typeof value === "string");
  const phase = new Map([
    ["connected to WA", "ws_open"],
    ["not logged in, attempting registration...", "registration"],
    ["logging in...", "login"],
    ["opened connection to WA", "connected"],
  ]).get(message);
  if (phase) transportPhase = phase;
  if (level !== "error") return;
  const details = args.find(value => value && typeof value === "object");
  const error = details?.err || details?.error || details?.uploadError;
  if (error) lastFailure = safeFailure(error, `baileys_${phase || "error"}`);
}

const diagnosticLogger = {
  level: "info",
  trace(...args) { captureBaileysLog("trace", args); },
  debug(...args) { captureBaileysLog("debug", args); },
  info(...args) { captureBaileysLog("info", args); },
  warn(...args) { captureBaileysLog("warn", args); },
  error(...args) { captureBaileysLog("error", args); },
  fatal(...args) { captureBaileysLog("fatal", args); },
  child() { return this; },
};

function resetLinkDiagnostics() {
  connectionUpdateCount = 0;
  lastConnectionEvent = "none";
  lastDisconnectCode = null;
  lastFailure = { phase: "", error_name: "", error_code: "", status_code: null };
  transportPhase = "idle";
  linkTimedOut = false;
}

function linkTimeoutMessage(phase) {
  if (["socket_start", "socket_created"].includes(phase)) {
    return "WhatsApp's network connection did not open. Check this host's internet and DNS access, then retry linking.";
  }
  if (phase === "ws_open") {
    return "WhatsApp's network connection opened, but its handshake did not finish. Retry linking.";
  }
  if (phase === "registration") {
    return "WhatsApp's handshake finished, but it did not provide a pairing QR code. Retry linking.";
  }
  return "WhatsApp did not provide a QR code. Retry linking.";
}

fs.mkdirSync(stateDir, { recursive: true, mode: 0o700 });
fs.chmodSync(stateDir, 0o700);

const store = loadStore();

function loadStore() {
  try {
    const parsed = JSON.parse(fs.readFileSync(storePath, "utf8"));
    if (parsed && typeof parsed === "object" && parsed.chats && parsed.messages) {
      parsed.contacts = parsed.contacts && typeof parsed.contacts === "object" ? parsed.contacts : {};
      parsed.aliases = parsed.aliases && typeof parsed.aliases === "object" ? parsed.aliases : {};
      return parsed;
    }
  } catch (_error) { /* an empty or interrupted cache is safe to rebuild */ }
  return { chats: {}, messages: {}, contacts: {}, aliases: {} };
}

function saveStoreSoon() {
  if (saveTimer) return;
  saveTimer = setTimeout(() => {
    saveTimer = null;
    writeStore();
  }, 500);
}

function writeStore() {
  const temporary = `${storePath}.tmp`;
  fs.writeFileSync(temporary, JSON.stringify(store), { mode: 0o600 });
  fs.renameSync(temporary, storePath);
}

function flushPendingStore() {
  if (!saveTimer) return;
  clearTimeout(saveTimer);
  saveTimer = null;
  writeStore();
}

function numberTimestamp(value) {
  let number;
  if (typeof value === "bigint") number = Number(value);
  else if (typeof value === "number") number = value;
  else if (value && typeof value.toNumber === "function") number = value.toNumber();
  else number = Number(value || 0);
  return Number.isFinite(number) ? Math.max(0, Math.trunc(number)) : 0;
}

function normalizedMessageBody(message) {
  let body = message || {};
  for (const wrapper of ["ephemeralMessage", "viewOnceMessage", "viewOnceMessageV2", "documentWithCaptionMessage"]) {
    if (body[wrapper] && body[wrapper].message) body = body[wrapper].message;
  }
  if (body.protocolMessage?.editedMessage) body = body.protocolMessage.editedMessage;
  return body;
}

function messageText(message) {
  const body = normalizedMessageBody(message);
  const text = body.conversation
    || body.extendedTextMessage?.text
    || body.imageMessage?.caption
    || body.videoMessage?.caption
    || body.documentMessage?.caption
    || body.buttonsResponseMessage?.selectedDisplayText
    || body.listResponseMessage?.title
    || "";
  return String(text).slice(0, MAX_TEXT);
}

function messageType(message) {
  const body = normalizedMessageBody(message);
  return Object.keys(body)[0] || "unknown";
}

function cachedMessageType(message) {
  const type = messageType(message);
  return type === "conversation" || type === "extendedTextMessage" ? "text" : type;
}

function upsertLidMapping(mapping) {
  const pn = String(mapping?.pn || "");
  const lid = String(mapping?.lid || "");
  if (!/^[A-Za-z0-9._:-]{1,128}@s\.whatsapp\.net$/.test(pn) || !/^[A-Za-z0-9._:-]{1,128}@lid$/.test(lid)) return;
  const previousLid = store.aliases[pn];
  const previousPn = store.aliases[lid];
  if (previousLid && previousLid !== lid) {
    if (store.aliases[previousLid] === pn) delete store.aliases[previousLid];
    delete store.aliases[pn];
  }
  if (previousPn && previousPn !== pn) {
    if (store.aliases[previousPn] === lid) delete store.aliases[previousPn];
    delete store.aliases[lid];
  }
  delete store.aliases[pn];
  delete store.aliases[lid];
  store.aliases[pn] = lid;
  store.aliases[lid] = pn;
  const unreadSource = [store.chats[pn], store.chats[lid]]
    .filter(Boolean)
    .sort((left, right) =>
      (right.last_message_at || 0) - (left.last_message_at || 0)
      || (right.unread_count || 0) - (left.unread_count || 0))[0];
  if (unreadSource) {
    for (const identifier of [pn, lid]) {
      if (store.chats[identifier]) store.chats[identifier].unread_count = unreadSource.unread_count;
    }
  }
  const name = store.contacts[pn] || store.contacts[lid] || store.chats[pn]?.name || store.chats[lid]?.name;
  if (name) {
    for (const identifier of [pn, lid]) {
      delete store.contacts[identifier];
      store.contacts[identifier] = name;
    }
    if (store.chats[pn]) store.chats[pn].name = name;
    if (store.chats[lid]) store.chats[lid].name = name;
  }
  trimConversationMessages(pn);
}

function chatIdentifiers(chatId) {
  const id = String(chatId || "");
  return [...new Set([id, store.aliases[id]].filter(identifier => READABLE_CHAT_RE.test(String(identifier))))];
}

function trimConversationMessages(chatId) {
  const identifiers = chatIdentifiers(chatId);
  const messagesById = new Map();
  for (const message of identifiers.flatMap(id => store.messages[id] || [])) {
    const previous = messagesById.get(message.id);
    if (!previous || message.timestamp > previous.timestamp) messagesById.set(message.id, message);
  }
  const retainedMessages = new Set(
    [...messagesById.values()]
      .sort((left, right) =>
        left.timestamp - right.timestamp
        || Number(left.chat_id === chatId) - Number(right.chat_id === chatId))
      .slice(-MAX_MESSAGES_PER_CHAT),
  );
  const storedIds = new Set();
  for (const id of identifiers) {
    if (Array.isArray(store.messages[id])) {
      store.messages[id] = store.messages[id].filter(message => {
        if (!retainedMessages.has(message) || storedIds.has(message.id)) return false;
        storedIds.add(message.id);
        return true;
      });
    }
  }
  if (retainedMessages.size) refreshChatFromMessages(chatId);
}

function upsertContact(contact) {
  if (!contact?.id) return;
  const id = String(contact.id);
  const name = String(contact.name || contact.notify || contact.verifiedName || "").slice(0, 200);
  upsertLidMapping({ pn: contact.phoneNumber, lid: contact.lid || (id.endsWith("@lid") ? id : "") });
  const identifiers = new Set([id, contact.phoneNumber, contact.lid, store.aliases[id]]);
  for (const identifier of identifiers) {
    if (!identifier || !READABLE_CHAT_RE.test(String(identifier))) continue;
    const retainedName = name || store.contacts[identifier] || "";
    delete store.contacts[identifier];
    store.contacts[identifier] = retainedName;
    if (name && store.chats[identifier]) store.chats[identifier].name = name;
  }
}

function upsertChat(chat, unreadIsDelta = false) {
  if (!chat?.id || !READABLE_CHAT_RE.test(String(chat.id))) return;
  const id = String(chat.id);
  const previous = store.chats[id] || {};
  const aliasChat = store.chats[store.aliases[id]] || {};
  const cachedUnread = Number(previous.unread_count ?? aliasChat.unread_count ?? 0);
  const incomingUnread = Number(chat.unreadCount);
  const unread = chat.unreadCount === undefined || chat.unreadCount === null
    ? cachedUnread
    : unreadIsDelta
      ? (incomingUnread > 0 ? cachedUnread + incomingUnread : 0)
      : incomingUnread;
  const unreadCount = Number.isFinite(unread) ? Math.max(0, Math.trunc(unread)) : 0;
  const name = String(chat.name || store.contacts[id] || previous.name || aliasChat.name || "").slice(0, 200);
  const updated = {
    id,
    name,
    last_message_at: numberTimestamp(chat.conversationTimestamp || chat.lastMessageRecvTimestamp || previous.last_message_at || aliasChat.last_message_at),
    unread_count: unreadCount,
    preview: String(previous.preview || aliasChat.preview || "").slice(0, 240),
  };
  delete store.chats[id];
  store.chats[id] = updated;
  if (chat.unreadCount !== undefined && chat.unreadCount !== null) {
    for (const identifier of chatIdentifiers(id)) {
      if (store.chats[identifier]) store.chats[identifier].unread_count = unreadCount;
    }
  }
  if (chat.name) {
    for (const identifier of chatIdentifiers(id)) {
      delete store.contacts[identifier];
      store.contacts[identifier] = name;
      if (store.chats[identifier]) store.chats[identifier].name = name;
    }
  }
}

function upsertMessage(item) {
  const rawJid = item?.key?.remoteJid;
  const rawId = item?.key?.id;
  if (!rawJid || !rawId || !READABLE_CHAT_RE.test(String(rawJid))) return;
  const jid = String(rawJid);
  const alternateJid = String(item.key.remoteJidAlt || "");
  upsertLidMapping({
    pn: jid.endsWith("@s.whatsapp.net") ? jid : alternateJid,
    lid: jid.endsWith("@lid") ? jid : alternateJid,
  });
  const id = String(rawId);
  const text = messageText(item.message);
  const timestamp = numberTimestamp(item.messageTimestamp);
  const message = {
    id,
    chat_id: jid,
    sender_id: String(item.key.participant || (item.key.fromMe ? account?.id || "me" : jid)),
    from_me: item.key.fromMe === true,
    timestamp,
    text,
    type: cachedMessageType(item.message),
  };
  const messages = Array.isArray(store.messages[jid]) ? store.messages[jid] : [];
  const index = messages.findIndex(existing => existing.id === id);
  if (index >= 0) messages[index] = message;
  else messages.push(message);
  messages.sort((left, right) => left.timestamp - right.timestamp);
  store.messages[jid] = messages.slice(-MAX_MESSAGES_PER_CHAT);
  trimConversationMessages(jid);
  upsertChat({ id: jid, conversationTimestamp: timestamp });
  refreshChatFromMessages(jid);
}

function refreshChatFromMessages(jid) {
  const identifiers = chatIdentifiers(jid);
  const messages = identifiers
    .flatMap(identifier => store.messages[identifier] || [])
    .sort((left, right) => left.timestamp - right.timestamp
      || Number(left.chat_id === jid) - Number(right.chat_id === jid));
  const latest = messages[messages.length - 1];
  for (const identifier of identifiers) {
    if (!store.chats[identifier]) continue;
    store.chats[identifier].preview = String(latest?.text || "").slice(0, 240);
    store.chats[identifier].last_message_at = numberTimestamp(latest?.timestamp);
  }
}

function updateCachedMessage(item) {
  const id = String(item?.key?.id || "");
  if (!id) return;
  const update = item?.update || {};
  for (const jid of chatIdentifiers(item?.key?.remoteJid)) {
    const messages = store.messages[jid];
    if (!Array.isArray(messages)) continue;
    const existing = messages.find(message => message.id === id);
    if (!existing) continue;
    if (update.message) {
      const text = messageText(update.message);
      existing.text = text;
      existing.type = cachedMessageType(update.message);
    }
    if (update.messageTimestamp !== undefined) {
      existing.timestamp = numberTimestamp(update.messageTimestamp);
      messages.sort((left, right) => left.timestamp - right.timestamp);
    }
    refreshChatFromMessages(jid);
  }
}

function deleteCachedMessages(event) {
  if (Array.isArray(event?.keys)) {
    const changed = new Set();
    for (const key of event.keys) {
      const id = String(key?.id || "");
      if (!id) continue;
      for (const jid of chatIdentifiers(key?.remoteJid)) {
        changed.add(jid);
        if (!Array.isArray(store.messages[jid])) continue;
        store.messages[jid] = store.messages[jid].filter(message => message.id !== id);
      }
    }
    for (const jid of changed) refreshChatFromMessages(jid);
  } else {
    for (const jid of event?.all === true ? chatIdentifiers(event?.jid) : []) {
      delete store.messages[jid];
      refreshChatFromMessages(jid);
    }
  }
}

function deleteCachedChat(chatId) {
  const identifiers = new Set(chatIdentifiers(chatId));
  for (const identifier of identifiers) {
    delete store.chats[identifier];
    delete store.messages[identifier];
  }
}

function trimAliases(chatIds = new Set(Object.keys(store.chats))) {
  const aliases = Object.entries(store.aliases)
    .filter(([id, alias]) => READABLE_CHAT_RE.test(id) && READABLE_CHAT_RE.test(String(alias)));
  const active = aliases.filter(([id, alias]) => chatIds.has(id) || chatIds.has(alias));
  const pending = aliases.filter(([id, alias]) => !chatIds.has(id) && !chatIds.has(alias));
  store.aliases = Object.fromEntries([
    ...active.slice(-MAX_CHATS * 2),
    ...pending.slice(-MAX_CHATS * 2),
  ]);
}

function trimChats() {
  const conversations = new Map();
  let chatOrder = 0;
  for (const chat of Object.values(store.chats)) {
    if (!READABLE_CHAT_RE.test(String(chat.id || ""))) continue;
    const identifiers = chatIdentifiers(chat.id);
    const key = [...identifiers].sort()[0] || chat.id;
    const conversation = conversations.get(key) || { identifiers: new Set(), last_message_at: 0, order: 0 };
    for (const identifier of identifiers) conversation.identifiers.add(identifier);
    conversation.last_message_at = Math.max(conversation.last_message_at, chat.last_message_at || 0);
    conversation.order = Math.max(conversation.order, chatOrder);
    conversations.set(key, conversation);
    chatOrder += 1;
  }
  const keep = [...conversations.values()]
    .sort((left, right) =>
      right.last_message_at - left.last_message_at || right.order - left.order)
    .slice(0, MAX_CHATS);
  const ids = new Set(keep.flatMap(conversation => [...conversation.identifiers]));
  for (const id of Object.keys(store.chats)) if (!ids.has(id)) delete store.chats[id];
  for (const id of Object.keys(store.messages)) if (!ids.has(id)) delete store.messages[id];
  const contactConversations = new Map();
  for (const id of Object.keys(store.contacts)) {
    if (!READABLE_CHAT_RE.test(id)) continue;
    const identifiers = chatIdentifiers(id);
    const key = [...identifiers].sort()[0] || id;
    const contacts = contactConversations.get(key) || new Set();
    for (const identifier of identifiers) {
      if (Object.hasOwn(store.contacts, identifier)) contacts.add(identifier);
    }
    contactConversations.set(key, contacts);
  }
  const activeContacts = [];
  const pendingContacts = [];
  for (const contacts of contactConversations.values()) {
    ([...contacts].some(identifier => ids.has(identifier)) ? activeContacts : pendingContacts)
      .push(contacts);
  }
  const remainingContacts = Math.max(0, MAX_CHATS - activeContacts.length);
  const retainedContacts = new Set(
    [...activeContacts, ...pendingContacts.slice(-remainingContacts)]
      .flatMap(contacts => [...contacts]),
  );
  for (const id of Object.keys(store.contacts)) if (!retainedContacts.has(id)) delete store.contacts[id];
  trimAliases(ids);
}

function clearLocalData() {
  if (saveTimer) clearTimeout(saveTimer);
  saveTimer = null;
  fs.rmSync(authDir, { recursive: true, force: true });
  fs.rmSync(storePath, { force: true });
  fs.rmSync(`${storePath}.tmp`, { force: true });
  store.chats = {};
  store.messages = {};
  store.contacts = {};
  store.aliases = {};
}

async function boundedLogout(activeSocket) {
  let timeout;
  try {
    await Promise.race([
      activeSocket.logout("Disconnected by the Kern operator").catch(() => undefined),
      new Promise(resolve => { timeout = setTimeout(resolve, LOGOUT_TIMEOUT_MS); }),
    ]);
  } finally {
    clearTimeout(timeout);
  }
}

function hasRetainedData() {
  return fs.existsSync(authDir) || fs.existsSync(storePath) || fs.existsSync(`${storePath}.tmp`);
}

function bindEvents(nextSocket, saveCreds) {
  const listeners = [];
  const listen = (event, handler) => {
    nextSocket.ev.on(event, handler);
    listeners.push([event, handler]);
  };
  listen("creds.update", () => {
    const pending = credentialWriteChain.then(() => saveCreds());
    credentialWriteChain = pending.catch(() => undefined);
    pendingCredentialWrites.add(pending);
    pending.then(
      () => pendingCredentialWrites.delete(pending),
      error => {
        pendingCredentialWrites.delete(pending);
        if (!manualLogout) closeActiveSocket(nextSocket, error, "credential_update");
      },
    );
  });
  listen("contacts.upsert", contacts => { contacts.forEach(upsertContact); trimChats(); saveStoreSoon(); });
  listen("contacts.update", contacts => { contacts.forEach(upsertContact); trimChats(); saveStoreSoon(); });
  listen("lid-mapping.update", mapping => { upsertLidMapping(mapping); trimChats(); saveStoreSoon(); });
  listen("chats.upsert", chats => { chats.forEach(upsertChat); trimChats(); saveStoreSoon(); });
  listen("chats.update", chats => { chats.forEach(chat => upsertChat(chat, true)); trimChats(); saveStoreSoon(); });
  listen("chats.delete", chatIds => { chatIds.forEach(deleteCachedChat); trimAliases(); saveStoreSoon(); });
  listen("messaging-history.set", history => {
    (history.lidPnMappings || []).forEach(upsertLidMapping);
    (history.contacts || []).forEach(upsertContact);
    (history.chats || []).forEach(upsertChat);
    (history.messages || []).forEach(upsertMessage);
    trimChats();
    saveStoreSoon();
  });
  listen("messages.upsert", event => {
    event.messages.forEach(upsertMessage);
    trimChats();
    saveStoreSoon();
  });
  listen("messages.update", updates => {
    updates.forEach(updateCachedMessage);
    trimChats();
    saveStoreSoon();
  });
  listen("messages.delete", event => {
    deleteCachedMessages(event);
    trimChats();
    saveStoreSoon();
  });
  listen("connection.update", async update => {
    connectionUpdateCount += 1;
    lastConnectionEvent = update.qr
      ? "qr"
      : ["open", "close"].includes(update.connection)
        ? update.connection
        : "other";
    if (update.qr) {
      transportPhase = "qr_received";
      const encodedGeneration = ++qrGeneration;
      try {
        const encoded = await qrcode.toDataURL(update.qr, { errorCorrectionLevel: "M", margin: 2, width: 320 });
        if (socket === nextSocket && encodedGeneration === qrGeneration) {
          qrDataUrl = encoded;
          status = "qr";
          transportPhase = "qr_ready";
        }
      } catch (error) {
        if (socket === nextSocket && encodedGeneration === qrGeneration) {
          closeActiveSocket(nextSocket, error, "qr_encoding");
        }
      }
    }
    if (update.connection === "open") {
      qrGeneration += 1;
      status = "connected";
      qrDataUrl = "";
      lastError = "";
      resetLinkDiagnostics();
      transportPhase = "connected";
      const user = nextSocket.user || {};
      account = { id: String(user.id || ""), label: String(user.name || user.id || "WhatsApp account") };
    }
    if (update.connection === "close") {
      if (socket !== nextSocket) return;
      qrGeneration += 1;
      qrDataUrl = "";
      const code = update.lastDisconnect?.error?.output?.statusCode;
      const numericCode = Number(code);
      lastDisconnectCode = Number.isInteger(numericCode) ? numericCode : null;
      lastFailure = safeFailure(update.lastDisconnect?.error, "connection_close");
      transportPhase = "closed";
      if (!manualLogout && code === DisconnectReason.loggedOut) {
        status = "disconnected";
        account = null;
        qrDataUrl = "";
        const cleanup = (async () => {
          await quiesceConnection();
          socket = null;
          clearLocalData();
        })();
        cleanupPromise = cleanup;
        try {
          await cleanup;
        } catch (error) {
          setConnectionError(error, "logout_cleanup");
        } finally {
          if (cleanupPromise === cleanup) cleanupPromise = null;
        }
        return;
      }
      socket = null;
      if (manualLogout) {
        status = "disconnected";
        account = null;
        qrDataUrl = "";
        return;
      }
      status = "connecting";
      lastError = "WhatsApp connection closed; reconnecting.";
      clearTimeout(reconnectTimer);
      reconnectTimer = setTimeout(() => ensureSocket().catch(error => setConnectionError(error, "reconnect")), 3000);
    }
  });
  return () => {
    for (const [event, handler] of listeners) nextSocket.ev.off(event, handler);
  };
}

function setConnectionError(error, phase = "connection") {
  status = "error";
  qrDataUrl = "";
  lastError = "WhatsApp connection failed. Retry linking or disconnect and link again.";
  lastFailure = safeFailure(error, phase);
  socket = null;
}

function closeActiveSocket(activeSocket, error, phase = "connection_event") {
  if (socket !== activeSocket) return;
  connectionGeneration += 1;
  qrGeneration += 1;
  if (unbindSocketEvents) unbindSocketEvents();
  unbindSocketEvents = null;
  try { activeSocket.end(undefined); } catch (_error) { /* already closed */ }
  socket = null;
  setConnectionError(error, phase);
}

async function ensureSocket() {
  if (socket) return;
  if (manualLogout) return;
  if (connectPromise) return connectPromise;
  const generation = connectionGeneration;
  connectPromise = (async () => {
    status = "connecting";
    transportPhase = "version_fetch";
    const { version } = await fetchLatestWaWebVersion({
      signal: AbortSignal.timeout(VERSION_FETCH_TIMEOUT_MS),
    });
    transportPhase = "socket_start";
    fs.mkdirSync(authDir, { recursive: true, mode: 0o700 });
    fs.chmodSync(authDir, 0o700);
    const { state, saveCreds } = await useMultiFileAuthState(authDir);
    if (generation !== connectionGeneration) return;
    const savedUser = state.creds?.me || {};
    if (state.creds?.registered && savedUser.id) {
      account = {
        id: String(savedUser.id),
        label: String(savedUser.name || savedUser.id || "WhatsApp account"),
      };
    }
    const nextSocket = makeWASocket({
      auth: state,
      logger: diagnosticLogger,
      markOnlineOnConnect: false,
      syncFullHistory: false,
      version,
    });
    if (transportPhase === "socket_start") transportPhase = "socket_created";
    if (generation !== connectionGeneration) {
      try { nextSocket.end(undefined); } catch (_error) { /* already closed */ }
      return;
    }
    socket = nextSocket;
    if (unbindSocketEvents) unbindSocketEvents();
    unbindSocketEvents = bindEvents(nextSocket, saveCreds);
  })();
  try { await connectPromise; }
  catch (error) {
    if (generation === connectionGeneration) {
      const failurePhase = transportPhase === "version_fetch" ? "version_fetch" : "socket_start";
      setConnectionError(error, failurePhase);
      if (failurePhase === "version_fetch") {
        lastError = "WhatsApp could not load its current Web version. Check this host's internet and DNS access, then retry linking.";
      }
    }
    throw new Error(lastError);
  }
  finally { connectPromise = null; }
}

async function quiesceConnection() {
  manualLogout = true;
  connectionGeneration += 1;
  qrGeneration += 1;
  clearTimeout(reconnectTimer);
  reconnectTimer = null;
  if (unbindSocketEvents) unbindSocketEvents();
  unbindSocketEvents = null;
  if (pendingCredentialWrites.size) {
    await Promise.allSettled([...pendingCredentialWrites]);
  }
  const pendingConnect = connectPromise;
  if (pendingConnect) await pendingConnect.catch(() => undefined);
}

function publicStatus() {
  return {
    status,
    connected: status === "connected",
    retained_data: hasRetainedData(),
    account,
    qr_data_url: qrDataUrl,
    error: lastError,
    diagnostic: {
      connection_updates: connectionUpdateCount,
      last_connection_event: lastConnectionEvent,
      last_disconnect_code: lastDisconnectCode,
      ...lastFailure,
      transport_phase: transportPhase,
      link_timed_out: linkTimedOut,
    },
  };
}

async function waitForLinkState() {
  await ensureSocket();
  const deadline = Date.now() + LINK_QR_TIMEOUT_MS;
  while (Date.now() < deadline && status === "connecting") {
    await new Promise(resolve => setTimeout(resolve, 200));
  }
  if (status === "connecting" && !account) {
    const timeoutError = new Error("WhatsApp did not provide a QR code.");
    const failureBeforeTimeout = lastFailure;
    const activeSocket = socket;
    await quiesceConnection();
    try { activeSocket?.end(undefined); } catch (_error) { /* already closed */ }
    socket = null;
    setConnectionError(timeoutError, "qr_timeout");
    if (
      failureBeforeTimeout.error_name
      || failureBeforeTimeout.error_code
      || failureBeforeTimeout.status_code !== null
    ) lastFailure = failureBeforeTimeout;
    linkTimedOut = true;
    lastError = linkTimeoutMessage(transportPhase);
  }
  return publicStatus();
}

function normalizeDirectChat(value) {
  const raw = String(value || "").trim();
  if (/^[1-9][0-9]{1,14}@s\.whatsapp\.net$/.test(raw)) return raw;
  const digits = raw.replace(/^\+/, "");
  if (!/^[1-9][0-9]{1,14}$/.test(digits)) throw new Error("Recipient must be an E.164 phone number, such as +447700900123.");
  return `${digits}@s.whatsapp.net`;
}

function normalizeReadableChat(value) {
  const raw = String(value || "").trim();
  if (READABLE_CHAT_RE.test(raw)) return raw;
  return normalizeDirectChat(raw);
}

async function dispatch(method, params) {
  if (method === "status") return publicStatus();
  if (method === "connect") {
    if (cleanupPromise) await cleanupPromise.catch(() => undefined);
    manualLogout = false;
    resetLinkDiagnostics();
    return waitForLinkState();
  }
  if (method === "shutdown") {
    // Preserve the linked-device auth state: this closes the live socket but
    // deliberately does not call logout. Flush only an already scheduled
    // cache write, so a disconnect that just cleared the cache cannot recreate
    // an empty messages file during the subsequent process stop.
    await quiesceConnection();
    try { socket?.end(undefined); } catch (_error) { /* already closed */ }
    socket = null;
    flushPendingStore();
    return { stopped: true };
  }
  if (method === "disconnect") {
    const disconnectedAccount = account;
    await quiesceConnection();
    const activeSocket = socket;
    try {
      // Stop adapter-owned persistence callbacks before logout. Baileys may
      // emit credential or history updates while closing; none may recreate
      // files after the operator requested authoritative local deletion.
      if (activeSocket) await boundedLogout(activeSocket);
    } finally {
      try { activeSocket?.end(undefined); } catch (_error) { /* already closed */ }
      socket = null;
      status = "disconnected";
      account = null;
      qrDataUrl = "";
      lastError = "";
      resetLinkDiagnostics();
      clearLocalData();
    }
    return { ...publicStatus(), account: disconnectedAccount };
  }
  if (method === "list_chats") {
    const limit = Math.max(1, Math.min(100, Number(params.limit || 20)));
    const byConversation = new Map();
    for (const chat of Object.values(store.chats)) {
      if (!READABLE_CHAT_RE.test(String(chat.id || ""))) continue;
      const key = chatIdentifiers(chat.id).sort()[0] || chat.id;
      const previous = byConversation.get(key);
      if (!previous || chat.last_message_at > previous.last_message_at) byConversation.set(key, chat);
    }
    const chats = [...byConversation.values()]
      .sort((left, right) => (right.last_message_at || 0) - (left.last_message_at || 0))
      .slice(0, limit);
    return { chats };
  }
  if (method === "read_messages") {
    const requestedChatId = normalizeReadableChat(params.chat_id);
    const messages = chatIdentifiers(requestedChatId)
      .flatMap(chatId => store.messages[chatId] || []);
    const merged = [...new Map(messages.map(message => [message.id, message])).values()]
      .sort((left, right) => left.timestamp - right.timestamp);
    const limit = Math.max(1, Math.min(100, Number(params.limit || 20)));
    return { chat_id: requestedChatId, messages: merged.slice(-limit) };
  }
  if (method === "send_message") {
    const expectedAccountId = String(params.account_id || "");
    const activeSocket = socket;
    if (status !== "connected" || !activeSocket) throw new Error("WhatsApp is not connected. Link it again in Home > Integrations.");
    if (!expectedAccountId || account?.id !== expectedAccountId) {
      throw new Error("The linked WhatsApp account changed after approval. Queue a new message.");
    }
    const jid = normalizeDirectChat(params.recipient);
    const phone = jid.split("@")[0];
    let matches;
    try { matches = await activeSocket.onWhatsApp(phone); }
    catch (_error) { throw new Error("WhatsApp could not verify that recipient. Retry with a new approval."); }
    const exists = Array.isArray(matches) && matches.some(item => item?.exists);
    if (!exists) throw new Error("That phone number is not registered on WhatsApp.");
    if (socket !== activeSocket || status !== "connected" || account?.id !== expectedAccountId) {
      throw new Error("The linked WhatsApp account changed after approval. Queue a new message.");
    }
    let result;
    try { result = await activeSocket.sendMessage(jid, { text: String(params.text) }); }
    catch (_error) { throw new Error("WhatsApp send outcome is unknown. Do not retry automatically; check the recipient chat first."); }
    return { message_id: String(result?.key?.id || ""), recipient: jid };
  }
  throw new Error("Unknown WhatsApp gateway operation.");
}

// Finish loading a retained auth state before accepting Disconnect. Otherwise
// the input handler could delete the auth directory while useMultiFileAuthState
// still has those credentials in flight and later writes them back.
if (fs.existsSync(path.join(authDir, "creds.json"))) {
  await ensureSocket().catch(error => setConnectionError(error, "startup_reconnect"));
} else if (hasRetainedData()) {
  // No credentials means there is no session to preserve. Remove any cache or
  // partial auth directory left by an interrupted provider logout.
  clearLocalData();
}

process.stdout.write(`${JSON.stringify({ ready: true })}\n`);

const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
input.on("line", async line => {
  let request;
  try {
    request = JSON.parse(line);
    const result = await dispatch(request.method, request.params || {});
    process.stdout.write(`${JSON.stringify({ id: request.id, ok: true, result })}\n`);
  } catch (error) {
    process.stdout.write(`${JSON.stringify({ id: request?.id, ok: false, error: String(error?.message || "WhatsApp gateway request failed.").slice(0, 500) })}\n`);
  }
});
