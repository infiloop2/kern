(() => {
"use strict";
// Draft storage shared by Chat and Apps. Consumers retain their key formats,
// textarea updates, and send state. Dictation shares this same drafts object.
window.KernComposerDrafts = function(storageKey) {
  const limit = 50;
  let drafts;
  try {
    drafts = JSON.parse(localStorage.getItem(storageKey) || "{}");
    if (!drafts || typeof drafts !== "object" || Array.isArray(drafts)) drafts = {};
  } catch (_error) {
    drafts = {};
  }

  function persist() {
    try {
      localStorage.setItem(storageKey, JSON.stringify(drafts));
    } catch (_error) {
      // Draft persistence is best-effort when browser storage is unavailable.
    }
  }

  function save(key, value) {
    delete drafts[key];
    if (value) drafts[key] = value;
    while (Object.keys(drafts).filter(key => key !== "__dictationReceipts").length > limit) {
      delete drafts[Object.keys(drafts).find(key => key !== "__dictationReceipts")];
    }
    persist();
  }

  function restore(key) {
    return drafts[key] || "";
  }

  function clear(key, submittedDraft) {
    if ((drafts[key] ?? "") !== submittedDraft) return false;
    delete drafts[key];
    persist();
    return true;
  }

  return { drafts, persist, save, restore, clear };
};
})();
