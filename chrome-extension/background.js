// MediaGrab fetcher — service worker: the only place allowed to talk to
// the native messaging host (mgd native-host).

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || msg.type !== "native") return false;
  chrome.runtime.sendNativeMessage(
    "com.alirizwantariq.mediagrab",
    msg.payload,
    resp => {
      const err = chrome.runtime.lastError;
      sendResponse(err ? { ok: false, error: err.message } : (resp || {ok: false, error: "no response"}));
    }
  );
  return true; // async response
});
