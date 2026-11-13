#!/usr/bin/env python3
"""Compile the production OPDS transport against storage and HTTP adapters."""
from pathlib import Path
import subprocess
import tempfile

root = Path(__file__).resolve().parents[2]
stubs = {
    "Arduino.h": (root / "freeink-sdk/libs/network/SecureNet/test/host/stubs/Arduino.h").read_text(),
    "util/TaskWatchdog.h": "#pragma once\ninline void resetTaskWatchdogIfSubscribed() { ++fake::watchdogResets; }\n",
    "HalMemory.h": "#pragma once\n#include <cstddef>\nnamespace fake { inline size_t largestBlock = 1000000; }\nstruct HalMemory { struct HeapStats { size_t freeBytes, largestBlockBytes; }; static HeapStats getDefaultHeap() { return {1000000, fake::largestBlock}; } static HeapStats getInternalHeap() { return getDefaultHeap(); } };\n",
    "Logging.h": "#pragma once\n#include <cstdio>\n#define LOG_ERR(tag, ...) (void)std::printf(__VA_ARGS__)\n#define LOG_DBG(tag, ...) (void)std::printf(__VA_ARGS__)\n",
    "util/UrlUtils.h": "#pragma once\n#include <string>\nnamespace UrlUtils { inline std::string buildUrl(const std::string&, const std::string& ref) { return ref; } }\n",
    "HalStorage.h": r'''
#pragma once
#include <algorithm>
#include <cstdint>
#include <cstring>
#include <string>
#include <Arduino.h>
inline constexpr int O_RDONLY = 0, O_WRONLY = 1, O_CREAT = 2, O_TRUNC = 4;
namespace fake {
inline std::string file, body = "<feed/>";
inline bool exists = true, network = false, complete = true;
inline bool requireBasic = false;
inline bool failOpen = false, shortWrite = false, failClose = false, shortRead = false, failRemove = false, failAlloc = false;
inline int status = 200, requests = 0, writes = 0;
inline int watchdogResets = 0;
}
class HalFile {
 bool opened = false;
 size_t pos = 0;
 public:
 HalFile() = default;
 explicit HalFile(bool ok) : opened(ok) {}
 explicit operator bool() const { return opened; }
 bool isOpen() const { return opened; }
 bool isDirectory() const { return false; }
 size_t fileSize() { return fake::file.size(); }
 uint64_t fileSize64() { return fake::file.size(); }
 size_t write(const uint8_t* data, size_t len) {
   assert(fake::network); ++fake::writes;
   if (fake::shortWrite) return 0;
   fake::file.append(reinterpret_cast<const char*>(data), len); return len;
 }
 int read(void* buf, size_t len) {
   assert(!fake::network);
   fakeNow() += 100;
   if (fake::shortRead) return 0;
   len = std::min(len, fake::file.size() - pos);
   std::memcpy(buf, fake::file.data() + pos, len); pos += len; return static_cast<int>(len);
 }
 bool close() { opened = false; return !fake::failClose; }
};
struct FakeStorage {
 bool exists(const char*) { return fake::exists; }
 bool remove(const char*) {
   if (fake::failRemove) return false;
   fake::exists = false; fake::file.clear(); return true;
 }
 bool ensureDirectoryExists(const char*) { return !fake::failOpen; }
 HalFile open(const char*, int flags = O_RDONLY) {
   if (fake::failOpen || (flags == O_RDONLY && !fake::exists)) return HalFile();
   if (flags != O_RDONLY) { fake::file.clear(); fake::exists = true; }
   return HalFile(true);
 }
};
inline FakeStorage fakeStorage;
#define Storage fakeStorage
''',
    "network/HttpDownloader.h": r'''
#pragma once
#include <OpdsTransport.h>
#include <HalStorage.h>
struct HttpDownloader {
 struct FetchOptions {
   std::string username, password, bearer, accept, acceptLanguage;
   std::vector<freeink::opds::HttpHeader> headers;
   bool captureErrorBody = false;
   int* statusOut = nullptr;
 };
 static bool fetchUrl(const std::string&, const freeink::opds::OpdsDataSink& sink, const FetchOptions& options) {
   ++fake::requests; fake::network = true;
   const int status = fake::requireBasic && options.username.empty() ? 401 : fake::status;
   *options.statusOut = status;
   bool accepted = true;
   if ((status >= 200 && status < 300) || (status == 401 && options.captureErrorBody))
     accepted = sink(reinterpret_cast<const uint8_t*>(fake::body.data()), fake::body.size());
   fake::network = false;
   return fake::complete && accepted && status >= 200 && status < 300;
 }
 static bool postForm(const std::string&, const std::string&, std::string&, int*) { return false; }
};
''',
}
test = r'''
#include <cassert>
#include "OpdsHttpTransport.h"
#include <HalStorage.h>
#include <HalMemory.h>
#include <new>
#include <cstdlib>
void* operator new(std::size_t bytes) {
  if (bytes > fake::largestBlock) throw std::bad_alloc();
  if (auto* value = std::malloc(bytes)) return value;
  throw std::bad_alloc();
}
void operator delete(void* value) noexcept { std::free(value); }
void operator delete(void* value, std::size_t) noexcept { ::operator delete(value); }
void* operator new[](std::size_t bytes) { return ::operator new(bytes); }
void operator delete[](void* value) noexcept { ::operator delete(value); }
void operator delete[](void* value, std::size_t) noexcept { ::operator delete(value); }
void* operator new[](std::size_t bytes, const std::nothrow_t&) noexcept {
  if (fake::failAlloc) return nullptr;
  try { return ::operator new[](bytes); } catch (...) { return nullptr; }
}
int main() {
 using Error = OpdsHttpTransport::FeedError;
 freeink::opds::HttpAuth auth;
 std::string received;
 auto sink = [&](const uint8_t* data, size_t len) {
   assert(!fake::network);
   received.append(reinterpret_cast<const char*>(data), len); return true;
 };
 {
   fake::file = "stale";
   OpdsHttpTransport transport;
   transport.invalidateFeed();
   assert(!fake::exists);
   fake::body = std::string(2500, 'x');
   auto result = transport.getFeed("/one", auth, nullptr, nullptr, false, sink);
   assert(result.ok && received == fake::body && fake::requests == 1);
   assert(fake::watchdogResets == 3);
   received.clear();
   assert(transport.getFeed("/one", auth, nullptr, nullptr, false, sink).ok);
   assert(received == fake::body && fake::requests == 1);
   assert(transport.getFeed("/two", auth, nullptr, nullptr, false, sink).ok && fake::requests == 2);
   transport.invalidateFeed();
   assert(!fake::exists);
   assert(transport.getFeed("/two", auth, nullptr, nullptr, false, sink).ok && fake::requests == 3);
 }
 assert(!fake::exists);
 {
   OpdsHttpTransport transport;
   const std::string firstUrl(600, 'a'), nextUrl(650, 'b');
   fake::body = "<feed/>";
   assert(transport.getFeed(firstUrl, auth, nullptr, nullptr, false, sink).ok);
   // Cached URL capacity must not double beyond the checked allocation size.
   fake::largestBlock = 1000;
   assert(transport.getFeed(nextUrl, auth, nullptr, nullptr, false, sink).ok);
   fake::largestBlock = 1000000;
 }
 for (int failure = 0; failure < 8; ++failure) {
   OpdsHttpTransport transport;
   received.clear();
   fake::complete = failure != 0;
   fake::shortWrite = failure == 1;
   fake::failOpen = failure == 2;
   fake::failClose = failure == 3;
   fake::shortRead = failure == 4;
   fake::status = failure == 5 ? 401 : 200;
   fake::failAlloc = failure == 7;
   auto reject = [](const uint8_t*, size_t) { return false; };
   const auto result = transport.getFeed("/failure", auth, nullptr, nullptr, false,
                                        failure == 6 ? freeink::opds::OpdsDataSink(reject) : sink);
   assert(!result.ok);
   if (failure < 4 || failure == 5) assert(received.empty());
   if (failure >= 1 && failure <= 4) assert(transport.feedError() == Error::StorageFailure);
   if (failure == 7) assert(transport.feedError() == Error::MemoryFailure);
   if (failure == 5) assert(result.status == 401 && transport.feedError() == Error::None);
   fake::complete = true; fake::shortWrite = fake::failOpen = fake::failClose = fake::shortRead = false;
   fake::status = 200; fake::failAlloc = false;
 }
 assert(!fake::exists);
 {
   OpdsHttpTransport transport;
   fake::file = "stale"; fake::exists = true; fake::failRemove = true;
   received.clear();
   assert(!transport.getFeed("/blocked", auth, nullptr, nullptr, false, sink).ok);
   assert(transport.feedError() == Error::StorageFailure && received.empty());
   fake::failRemove = false;
   transport.invalidateFeed();
   // Non-feed authentication GETs retain streaming semantics.
   fake::status = 401;
   bool streamed = false;
   auto authSink = [&](const uint8_t*, size_t) { streamed = fake::network; return true; };
   assert(transport.get("/auth", auth, nullptr, nullptr, true, authSink).status == 401 && streamed);
 }
}
'''
integration = r'''
#include <cassert>
#include <OpdsClient.h>
#include "OpdsHttpTransport.h"
#include <HalStorage.h>
int main() {
  using Status = freeink::opds::OpdsClient::FetchStatus;
  OpdsHttpTransport transport;
  assert(transport.invalidateFeed());
  freeink::opds::OpdsClient client(transport);
  client.setServer("/feed", "user", "password");
  fake::body = "<feed>";
  for (int i = 0; i < 50; ++i)
    fake::body += "<entry><title>Book</title><summary>" + std::string(500, 'd') +
                  "</summary><link type=\"application/atom+xml\" href=\"/" + std::to_string(i) + "\"/></entry>";
  fake::body += "<link rel=\"next\" href=\"/server2\"/></feed>";
  fake::requireBasic = true;
  size_t offset = 0, books = 0;
  do {
    OpdsFeedParser parser;
    parser.setWindow({offset, SIZE_MAX, 128});
    assert(client.fetchFeed("/feed", parser) == Status::Ok);
    for (const auto& entry : parser.takeEntries()) {
      assert(entry.href == "/" + std::to_string(books++));
      assert(entry.description.size() == 500);
    }
    assert(parser.getNextPageUrl() == "/server2");
    assert(fake::requests == 3); // initial 401, Basic probe, authenticated feed
    if (!parser.hasMoreEntries()) break;
    assert(parser.nextEntryOffset() > offset);
    offset = parser.nextEntryOffset();
  } while (true);
  assert(books == 50);
  // Retry invalidates the snapshot; interrupted bodies never reach the parser.
  assert(transport.invalidateFeed());
  fake::complete = false;
  OpdsFeedParser interrupted;
  assert(client.fetchFeed("/feed", interrupted) == Status::FetchFailed);
  assert(!interrupted.hasEntries() && !fake::exists);
}
'''
with tempfile.TemporaryDirectory(prefix="crosspoint-opds-staging-") as directory:
    temp = Path(directory)
    for name, content in stubs.items():
        path = temp / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    cpp = temp / "test.cpp"
    cpp.write_text(test)
    binary = str(temp / "test")
    subprocess.run(["c++", "-std=c++20", "-Wall", "-Wextra", "-Werror", "-fsanitize=address,undefined",
                    "-include", "cassert", "-include", "HalStorage.h", "-I" + str(temp), "-I" + str(root / "src/network"),
                    "-I" + str(root / "lib/Memory"),
                    "-I" + str(root / "freeink-sdk/libs/network/Opds/include"),
                    str(cpp), str(root / "src/network/OpdsHttpTransport.cpp"), "-o", binary], check=True)
    subprocess.run([binary], check=True)
    cpp.write_text(integration)
    sdk = root / "freeink-sdk"
    includes = [temp, root / "src/network", root / "lib/Memory", sdk / "libs/network/Opds/test/host/stubs",
                sdk / "libs/network/SecureNet/test/host/stubs", sdk / "libs/network/Opds/include",
                sdk / "libs/network/JsonSax/include"]
    subprocess.run(["c++", "-std=c++20", "-Wall", "-Wextra", "-fsanitize=address,undefined", "-include", "cassert",
                    *["-I" + str(path) for path in includes], str(cpp),
                    str(root / "src/network/OpdsHttpTransport.cpp"),
                    *map(str, sorted((sdk / "libs/network/Opds/src").glob("*.cpp"))),
                    str(sdk / "libs/network/JsonSax/src/StreamingJsonParser.cpp"),
                    "-lexpat", "-o", binary], check=True)
    subprocess.run([binary], check=True)
print("OPDS staging: TLS lifetime, replay, invalidation, storage/network failures and authentication passed")
