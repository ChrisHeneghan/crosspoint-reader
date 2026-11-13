#!/usr/bin/env python3
"""Run the activity's production navigation methods with hardware-free adapters."""
from pathlib import Path
import subprocess
import tempfile

root = Path(__file__).resolve().parents[2]
source = (root / "src/activities/browser/OpdsBookBrowserActivity.cpp").read_text()
header = (root / "src/activities/browser/OpdsBookBrowserActivity.h").read_text()
def method(text, signature):
    start = text.index(signature)
    opening = text.index("{", start)
    depth = 1
    end = opening + 1
    while depth:
        if text[end] == "{": depth += 1
        elif text[end] == "}": depth -= 1
        end += 1
    return text[start:end]

# Reuse the SDK's allocation adapter so retained browser history consumes the
# same heap budget as parsing. Fixture bytes model the external staged file.
sdk_test = (root / "freeink-sdk/libs/network/Opds/test/host/test_feed_windows.cpp").read_text()
allocation_adapter = sdk_test[:sdk_test.index("std::string atomEntry")]
adapter = allocation_adapter + r"""
#include <OpdsClient.h>
#include <array>
#define LOG_ERR(...)
#define LOG_DBG(...)
enum class StrId { STR_MEMORY_ERROR, STR_OPDS_FEED_STORAGE_ERROR };
namespace HalMemory {
struct HeapStats { size_t freeBytes, largestBlockBytes; };
HeapStats getDefaultHeap() {
  auto& memory = freeink::MemoryManager::instance();
  return {memory.freeBytes(), memory.largestFreeBlock()};
}
HeapStats getInternalHeap() {
  auto& memory = freeink::MemoryManager::instance();
  return {memory.freeBytes(freeink::MemPool::Internal), memory.largestFreeBlock(freeink::MemPool::Internal)};
}
}
constexpr unsigned long GO_HOME_MS = 1000;
namespace UrlUtils {
std::string buildUrl(const std::string& base, const std::string& reference) {
  return reference.empty() ? base : reference;
}
}
const std::string* stagedBody = nullptr;
class Transport : public freeink::opds::OpdsTransport {
 public:
  int replays = 0;
  bool requireBasic = false;
  bool invalidateFeed() { return true; }
  freeink::opds::HttpResult get(const std::string&, const freeink::opds::HttpAuth& auth, const char*, const char*,
                               bool capture, const freeink::opds::OpdsDataSink& sink) override {
    ++replays;
    if (requireBasic && auth.username.empty()) {
      if (capture) sink(reinterpret_cast<const uint8_t*>(""), 0);
      return {false, 401};
    }
    for (size_t i = 0; i < stagedBody->size(); i += 1024)
      if (!sink(reinterpret_cast<const uint8_t*>(stagedBody->data() + i), std::min(size_t(1024), stagedBody->size() - i)))
        return {false, 200};
    return {true, 200};
  }
  bool postForm(const std::string&, const std::string&, std::string&, int&) override { return false; }
  std::string resolveUrl(const std::string&, const std::string& ref) override { return ref; }
};
class OpdsBookBrowserActivity {
 public:
  static constexpr size_t MAX_NAVIGATION_HISTORY = 16;
  static constexpr size_t MAX_HISTORY_BYTES = 8 * 1024;
  struct BrowsePosition { std::string path, query; OpdsWindowRequest window; bool forwardPage; };
  std::vector<BrowsePosition> navigationHistory;
  std::vector<OpdsEntry> entries, supplementaryEntries;
  std::string currentPath, searchQuery, headerSearchTitle;
  std::string pageNextHref, pagePrevHref, pageFirstHref, pageLastHref;
  struct { std::string url; } server;
  Transport opdsTransport;
  freeink::opds::OpdsClient opdsClient{opdsTransport};
  struct { unsigned long held = 0; unsigned long getHeldTime() { return held; } } mappedInput;
  OpdsWindowRequest feedWindow;
  size_t nextEntryOffset = 0, visibleEntryStart = 0;
  bool moreFeedEntries = false, home = false, previousWindowAvailable = false;
  int loads = 0;
  int tokenPersists = 0;
  freeink::opds::OpdsClient::FetchStatus lastStatus = freeink::opds::OpdsClient::FetchStatus::Ok;
  bool loadFeeds = false;
  void startBrowse();
  void onGoHome() { home = true; }
  bool savePosition(bool);
  void fail(StrId) { memoryError = true; }
  bool memoryError = false;
  bool persistTokens() { ++tokenPersists; return true; }
  void nextPage();
  void retryBrowse();
  void previousPage();
  void followPageLink(const std::string&, bool, OpdsWindowMode mode = OpdsWindowMode::Forward);
  void navigateToEntry(const OpdsEntry&, bool forwardPage = false, OpdsWindowMode mode = OpdsWindowMode::Forward);
  void navigateBack();
  void onBackButton();
  void setSearchQuery(const std::string&);
  freeink::opds::OpdsClient::FetchStatus fetchWindow(const std::string&, OpdsWindowRequest&, OpdsFeedParser&);
"""
for name in ("hasNextPage", "hasPreviousPage", "isPaginationHref", "entryCount", "entryAt"):
    adapter += method(header, ("size_t " if name == "entryCount" else "const OpdsEntry& " if name == "entryAt" else "bool ") + name)
adapter += "\n};\n"
for name in ("savePosition", "nextPage", "previousPage", "followPageLink", "navigateToEntry", "navigateBack", "onBackButton", "setSearchQuery", "retryBrowse"):
    adapter += method(source, ("bool" if name == "savePosition" else "void") + " OpdsBookBrowserActivity::" + name) + "\n"
adapter += method(source, "freeink::opds::OpdsClient::FetchStatus OpdsBookBrowserActivity::fetchWindow")
adapter += r"""
void OpdsBookBrowserActivity::startBrowse() {
  ++loads;
  if (!loadFeeds) return;
  std::vector<OpdsEntry>().swap(entries);
  OpdsFeedParser parser;
  auto request = feedWindow;
  request.bytesPerRow = 128;
  lastStatus = fetchWindow(currentPath, request, parser);
  if (lastStatus != freeink::opds::OpdsClient::FetchStatus::Ok) {
    memoryError = lastStatus == freeink::opds::OpdsClient::FetchStatus::MemoryError;
    return;
  }
  feedWindow = request;
  visibleEntryStart = request.start;
  nextEntryOffset = parser.nextEntryOffset();
  feedWindow.end = nextEntryOffset;
  moreFeedEntries = parser.hasMoreEntries();
  previousWindowAvailable = request.start > 0 || (!navigationHistory.empty() && navigationHistory.back().forwardPage);
  pageNextHref = parser.getNextPageUrl();
  pagePrevHref = parser.getPrevPageUrl();
  entries = parser.takeEntries();
}
std::string fixture(bool json, int count, bool mixed = false) {
  std::string out = json ? "{\"publications\":[" : "<feed>";
  for (int i = 0; i < count; ++i) {
    const bool large = mixed && i % 5 == 0;
    const std::string title(large ? 160 : 4, 't');
    const std::string description(!mixed || large ? 500 : 20, 'd');
    const std::string author(large ? 120 : 1, 'a');
    const std::string id(large ? 128 : 1, 'i');
    const std::string href = "/book/" + std::to_string(i) + std::string(large ? 720 : 0, 'u');
    if (json) {
      if (i) out += ',';
      out += "{\"metadata\":{\"title\":\"" + title + "\",\"description\":\"" + description +
             "\",\"author\":\"" + author + "\",\"identifier\":\"" + id +
             "\"},\"links\":[{\"rel\":\"acquisition\",\"type\":\"application/epub+zip\",\"href\":\"" + href + "\"}]}";
    } else {
      out += "<entry><title>" + title + "</title><summary>" + description + "</summary><author><name>" + author +
             "</name></author><id>" + id + "</id><link type=\"application/atom+xml\" href=\"" + href + "\"/></entry>";
    }
  }
  return out + (json ? "]}" : "</feed>");
}
int main() {
  for (bool json : {false, true}) {
    const auto body = fixture(json, 1300);
    stagedBody = &body;
    freeink::testBaseline = freeink::testAllocated;
    freeink::testAccounting = true;
    freeink::testFree = freeink::testInternalFree = 85000;
    freeink::testBlock = freeink::testInternalBlock = 49140;
    {
      OpdsBookBrowserActivity a;
      a.currentPath = std::string(78, 'p');
      const std::string parent = a.currentPath;
      OpdsEntry feed; feed.href = std::string(78, 'f');
      a.navigateToEntry(feed);
      assert(a.navigationHistory.size() == 1);
      a.loadFeeds = true;
      a.startBrowse();
      assert(a.lastStatus == freeink::opds::OpdsClient::FetchStatus::Ok);
      std::array<std::pair<size_t, size_t>, 512> ranges;
      size_t windows = 0, books = 0, retainedBytes = 0;
      do {
        ranges[windows++] = {a.feedWindow.start, a.feedWindow.end};
        for (const auto& entry : a.entries) assert(entry.href == "/book/" + std::to_string(books++));
        std::vector<OpdsEntry>().swap(a.entries);
        if (windows == 1) retainedBytes = freeink::testAllocated;
        assert(freeink::testAllocated <= retainedBytes + 64);
        assert(a.navigationHistory.size() == 1 && a.navigationHistory.capacity() == 16);
        if (!a.moreFeedEntries) break;
        a.nextPage();
        assert(a.lastStatus == freeink::opds::OpdsClient::FetchStatus::Ok);
      } while (true);
      assert(books == 1300 && windows > 129);
      // Backward scans use fixed scalar cursors; their production fetchWindow performs both passes.
      for (size_t i = windows - 1; i > 0; --i) {
        a.onBackButton();
        assert(a.lastStatus == freeink::opds::OpdsClient::FetchStatus::Ok);
        assert(a.feedWindow.start == ranges[i - 1].first && a.feedWindow.end == ranges[i - 1].second);
        assert(a.navigationHistory.size() == 1);
      }
      assert(a.feedWindow.start == 0 && !a.hasPreviousPage());
      a.loadFeeds = false;
      a.onBackButton();
      assert(a.currentPath == parent && a.navigationHistory.empty());
    }
    freeink::testAccounting = false;
    freeink::testFree = freeink::testInternalFree = 1000000;
    freeink::testBlock = freeink::testInternalBlock = 1000000;
  }
  for (bool json : {false, true}) {
    const auto body = fixture(json, 80, true);
    stagedBody = &body;
    OpdsBookBrowserActivity a;
    a.currentPath = "/mixed";
    a.loadFeeds = true;
    a.startBrowse();
    std::array<std::pair<size_t, size_t>, 80> ranges;
    size_t count = 0;
    do {
      assert(a.lastStatus == freeink::opds::OpdsClient::FetchStatus::Ok);
      ranges[count++] = {a.feedWindow.start, a.feedWindow.end};
      if (!a.moreFeedEntries) break;
      a.nextPage();
    } while (true);
    for (size_t i = count - 1; i > 0; --i) {
      a.previousPage();
      assert(a.lastStatus == freeink::opds::OpdsClient::FetchStatus::Ok);
      assert(a.feedWindow.start == ranges[i - 1].first && a.feedWindow.end == ranges[i - 1].second);
    }
    assert(a.navigationHistory.empty());
  }
  {
    const auto body = fixture(false, 50);
    stagedBody = &body;
    OpdsBookBrowserActivity a;
    a.currentPath = "/parent";
    OpdsEntry entry; entry.href = "/feed";
    a.navigateToEntry(entry);
    a.loadFeeds = true;
    a.feedWindow = {20, SIZE_MAX, 128, OpdsWindowMode::Previous};
    freeink::testFree = 1;
    a.startBrowse();
    assert(a.memoryError && a.navigationHistory.size() == 1);
    freeink::testFree = 1000000;
    freeink::testInternalFree = 1;
    assert(!a.savePosition(false) && a.navigationHistory.size() == 1);
    freeink::testInternalFree = 1000000;
    a.loadFeeds = false;
    a.onBackButton(); assert(a.currentPath == "/parent");
    a.opdsClient.setServer("/feed", "user", "password");
    a.opdsTransport.requireBasic = true;
    a.currentPath = "/feed";
    a.feedWindow = {SIZE_MAX, SIZE_MAX, 128, OpdsWindowMode::Previous};
    a.loadFeeds = true;
    a.startBrowse();
    assert(a.lastStatus == freeink::opds::OpdsClient::FetchStatus::Ok && !a.entries.empty());
    assert(a.tokenPersists == 1 && a.feedWindow.mode == OpdsWindowMode::Forward);
    a.retryBrowse(); assert(a.feedWindow.start == 0 && a.navigationHistory.empty());
  }
  {
    OpdsBookBrowserActivity a;
    a.currentPath = "/parent";
    a.setSearchQuery("query");
    a.feedWindow = {10, 17, 0}; a.moreFeedEntries = false; a.pageNextHref = "/server2";
    a.nextPage();
    assert(a.currentPath == "/server2" && a.searchQuery == "query" && a.navigationHistory.size() == 1);
    a.previousPage();
    assert(a.currentPath == "/parent" && a.feedWindow.start == 10 && a.feedWindow.end == 17);
    a.retryBrowse(); assert(a.feedWindow.start == 0 && a.feedWindow.mode == OpdsWindowMode::Forward);
    a.pagePrevHref = "/older";
    a.previousPage();
    assert(a.currentPath == "/older" && a.feedWindow.mode == OpdsWindowMode::Previous && a.feedWindow.start == SIZE_MAX);
    // A failed preceding-range scan can leave that URL through Back instead of retrying forever.
    a.navigateBack(); assert(a.currentPath == "/parent");
  }
  {
    OpdsBookBrowserActivity a;
    a.currentPath = "/zero";
    for (int i = 0; i < 200; ++i) {
      OpdsEntry entry; entry.href = "/category/" + std::to_string(i);
      a.navigateToEntry(entry);
      assert(a.navigationHistory.size() <= 16 && a.navigationHistory.capacity() == 16);
    }
    for (int i = 0; i < 16; ++i) a.navigateBack();
    assert(a.navigationHistory.empty() && !a.home);
    a.navigateBack(); assert(a.home);
  }
  {
    OpdsBookBrowserActivity a;
    for (int i = 0; i < 32; ++i) {
      a.currentPath = std::string(i % 2 ? 1200 : 4, 'u');
      a.setSearchQuery(i % 3 ? "" : "a query");
      assert(a.savePosition(false));
      size_t bytes = a.navigationHistory.capacity() * sizeof(OpdsBookBrowserActivity::BrowsePosition) + 32;
      for (const auto& p : a.navigationHistory) bytes += p.path.capacity() + p.query.capacity() + 66;
      assert(bytes <= 8192 && a.navigationHistory.size() <= 16);
    }
    const size_t count = a.navigationHistory.size();
    freeink::testFree = 1;
    assert(!a.savePosition(false) && a.memoryError && a.navigationHistory.size() == count);
    freeink::testFree = 1000000;
    a.mappedInput.held = GO_HOME_MS; a.onBackButton(); assert(a.home);
  }
  puts("OPDS navigation: 1300 Atom/JSON entries both ways, constant local state and bounded navigation history passed");
}
"""
with tempfile.TemporaryDirectory(prefix="crosspoint-opds-navigation-") as directory:
    cpp = Path(directory) / "test.cpp"
    cpp.write_text(adapter)
    binary = str(Path(directory) / "test")
    sdk = root / "freeink-sdk"
    includes = [sdk / "libs/network/Opds/test/host/stubs", sdk / "libs/network/SecureNet/test/host/stubs",
                sdk / "libs/network/Opds/include", sdk / "libs/network/JsonSax/include"]
    subprocess.run(["c++", "-std=c++17", "-Wall", "-Wextra", "-fsanitize=address,undefined",
                    *["-I" + str(path) for path in includes], str(cpp),
                    *map(str, sorted((sdk / "libs/network/Opds/src").glob("*.cpp"))),
                    str(sdk / "libs/network/JsonSax/src/StreamingJsonParser.cpp"), "-lexpat", "-o", binary], check=True)
    subprocess.run([binary], check=True)
