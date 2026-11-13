#include "OpdsHttpTransport.h"

#include <Arduino.h>
#include <HalMemory.h>
#include <HalStorage.h>
#include <Logging.h>
#include <Memory.h>

#include <algorithm>

#include "network/HttpDownloader.h"
#include "util/TaskWatchdog.h"
#include "util/UrlUtils.h"

namespace fo = freeink::opds;

OpdsHttpTransport::~OpdsHttpTransport() { invalidateFeed(); }

bool OpdsHttpTransport::invalidateFeed() {
  std::string().swap(feedUrl);
  feedBytes = 0;
  feedStatus = 0;
  if (Storage.exists(FEED_PATH) && !Storage.remove(FEED_PATH)) {
    LOG_ERR("OPDS", "Cannot remove staged feed");
    lastFeedError = FeedError::StorageFailure;
    return false;
  }
  return true;
}

fo::HttpResult OpdsHttpTransport::getFeed(const std::string& url, const fo::HttpAuth& auth, const char* accept,
                                          const char* acceptLanguage, bool captureErrorBody,
                                          const fo::OpdsDataSink& onChunk) {
  lastFeedError = FeedError::None;
  if (feedStatus && feedUrl == url) return replayFeed(onChunk);
  if (!invalidateFeed()) return {false, 0};
  const auto beforeDownload = HalMemory::getInternalHeap();
  LOG_DBG("OPDS", "Before feed download: internal free=%u largest=%u", static_cast<unsigned>(beforeDownload.freeBytes),
          static_cast<unsigned>(beforeDownload.largestBlockBytes));
  if (!Storage.ensureDirectoryExists("/.crosspoint")) {
    LOG_ERR("OPDS", "Cannot create feed directory");
    lastFeedError = FeedError::StorageFailure;
    return {false, 0};
  }
  auto file = Storage.open(FEED_PATH, O_WRONLY | O_CREAT | O_TRUNC);
  if (!file) {
    LOG_ERR("OPDS", "Cannot open staged feed for writing");
    lastFeedError = FeedError::StorageFailure;
    return {false, 0};
  }
  bool completed = false;
  ScopedCleanup cleanup{[&] {
    if (file.isOpen()) file.close();
    if (!completed) invalidateFeed();
  }};
  const auto result = get(url, auth, accept, acceptLanguage, captureErrorBody, [&](const uint8_t* data, size_t len) {
    if (file.write(data, len) != len) {
      LOG_ERR("OPDS", "Short staged feed write: %u bytes", static_cast<unsigned>(len));
      lastFeedError = FeedError::StorageFailure;
      return false;
    }
    feedBytes += len;
    return true;
  });
  // get() has returned: its HTTP client and TLS allocations are gone.
  if (!file.close()) {
    LOG_ERR("OPDS", "Cannot close staged feed writer");
    lastFeedError = FeedError::StorageFailure;
    return {false, result.status};
  }
  if (!result.ok) return result;
  const auto heap = HalMemory::getDefaultHeap();
  if (heap.largestBlockBytes < url.size() + 1 || heap.freeBytes <= url.size() + 2048) {
    LOG_ERR("OPDS", "Cannot retain staged feed URL: %u bytes", static_cast<unsigned>(url.size() + 1));
    lastFeedError = FeedError::MemoryFailure;
    return {false, result.status};
  }
  feedStatus = result.status;
  feedUrl = url;
  completed = true;
  return replayFeed(onChunk);
}

fo::HttpResult OpdsHttpTransport::replayFeed(const fo::OpdsDataSink& onChunk) {
  const auto beforeReplay = HalMemory::getInternalHeap();
  LOG_DBG("OPDS", "Before feed replay: internal free=%u largest=%u", static_cast<unsigned>(beforeReplay.freeBytes),
          static_cast<unsigned>(beforeReplay.largestBlockBytes));
  auto file = Storage.open(FEED_PATH);
  bool completed = false;
  const int status = feedStatus;
  ScopedCleanup cleanup{[&] {
    if (file.isOpen()) file.close();
    if (!completed) invalidateFeed();
  }};
  if (!file || file.isDirectory() || file.fileSize64() != feedBytes) {
    LOG_ERR("OPDS", "Cannot read complete staged feed");
    lastFeedError = FeedError::StorageFailure;
    return {false, status};
  }
  if (!replayBuffer) replayBuffer = makeUniqueNoThrow<uint8_t[]>(REPLAY_BYTES);
  if (!replayBuffer) {
    LOG_ERR("OPDS", "Cannot allocate %u replay bytes", static_cast<unsigned>(REPLAY_BYTES));
    lastFeedError = FeedError::MemoryFailure;
    return {false, status};
  }
  uint64_t remaining = feedBytes;
  uint32_t lastServiceMs = millis();
  while (remaining) {
    const size_t wanted = static_cast<size_t>(std::min<uint64_t>(remaining, REPLAY_BYTES));
    if (file.read(replayBuffer.get(), wanted) != static_cast<int>(wanted)) {
      LOG_ERR("OPDS", "Short staged feed read: %u bytes", static_cast<unsigned>(wanted));
      lastFeedError = FeedError::StorageFailure;
      return {false, status};
    }
    if (!onChunk(replayBuffer.get(), wanted)) return {false, status};
    remaining -= wanted;
    if (millis() - lastServiceMs >= 100) {
      resetTaskWatchdogIfSubscribed();
      delay(1);
      lastServiceMs = millis();
    }
  }
  if (!file.close()) {
    LOG_ERR("OPDS", "Cannot close staged feed reader");
    lastFeedError = FeedError::StorageFailure;
    return {false, status};
  }
  completed = true;
  return {true, status};
}

fo::HttpResult OpdsHttpTransport::get(const std::string& url, const fo::HttpAuth& auth, const char* accept,
                                      const char* acceptLanguage, const bool captureErrorBody,
                                      const fo::OpdsDataSink& onChunk) {
  int status = 0;
  HttpDownloader::FetchOptions options;
  options.username = auth.username;
  options.password = auth.password;
  options.bearer = auth.bearer;
  options.headers = auth.headers;
  if (accept) options.accept = accept;
  if (acceptLanguage) options.acceptLanguage = acceptLanguage;
  options.captureErrorBody = captureErrorBody;
  options.statusOut = &status;
  const bool ok = HttpDownloader::fetchUrl(url, onChunk, options);
  return fo::HttpResult{ok, status};
}

bool OpdsHttpTransport::postForm(const std::string& url, const std::string& body, std::string& outResponse,
                                 int& outStatus) {
  return HttpDownloader::postForm(url, body, outResponse, &outStatus);
}

std::string OpdsHttpTransport::resolveUrl(const std::string& base, const std::string& ref) {
  return UrlUtils::buildUrl(base, ref);
}
