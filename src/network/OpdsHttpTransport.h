#pragma once
#include <OpdsTransport.h>

#include <memory>

/**
 * Firmware implementation of the SDK OpdsClient's transport: streaming GET and
 * form POST over HttpDownloader (esp_http_client + wolfSSL), and URL joining
 * through UrlUtils. Owns one staged feed for the browser's local windows.
 */
class OpdsHttpTransport final : public freeink::opds::OpdsTransport {
 public:
  enum class FeedError { None, StorageFailure, MemoryFailure };
  ~OpdsHttpTransport() override;

  freeink::opds::HttpResult getFeed(const std::string& url, const freeink::opds::HttpAuth& auth, const char* accept,
                                    const char* acceptLanguage, bool captureErrorBody,
                                    const freeink::opds::OpdsDataSink& onChunk) override;
  bool invalidateFeed();
  FeedError feedError() const { return lastFeedError; }
  freeink::opds::HttpResult get(const std::string& url, const freeink::opds::HttpAuth& auth, const char* accept,
                                const char* acceptLanguage, bool captureErrorBody,
                                const freeink::opds::OpdsDataSink& onChunk) override;

  bool postForm(const std::string& url, const std::string& body, std::string& outResponse, int& outStatus) override;

  std::string resolveUrl(const std::string& base, const std::string& ref) override;

 private:
  static constexpr const char* FEED_PATH = "/.crosspoint/opds-feed.tmp";
  static constexpr size_t REPLAY_BYTES = 1024;
  std::unique_ptr<uint8_t[]> replayBuffer;
  std::string feedUrl;
  uint64_t feedBytes = 0;
  int feedStatus = 0;
  FeedError lastFeedError = FeedError::None;
  freeink::opds::HttpResult replayFeed(const freeink::opds::OpdsDataSink& onChunk);
};
