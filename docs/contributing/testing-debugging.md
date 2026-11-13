# Testing and Debugging

CrossPoint runs on real hardware, so debugging usually combines local build checks and on-device logs.

## Local checks

Make sure `clang-format` 21+ is installed and available in `PATH` before running the formatting step.
If needed, see [Getting Started](./getting-started.md).

```sh
./bin/clang-format-fix
pio check --fail-on-defect low --fail-on-defect medium --fail-on-defect high
pio run
```

## Flash and monitor

Flash firmware:

```sh
pio run --target upload
```

Open serial monitor:

```sh
pio device monitor
```

Optional enhanced monitor:

```sh
python3 -m pip install pyserial colorama matplotlib
python3 scripts/debugging_monitor.py
```

## Useful bug report contents

- Firmware version and build environment
- Exact steps to reproduce
- Expected vs actual behavior
- Serial logs from boot through failure
- Whether issue reproduces after clearing `.crosspoint/` cache on SD card

## Common troubleshooting references

- [User Guide troubleshooting section](../../USER_GUIDE.md#7-troubleshooting-issues--escaping-bootloop)
- [Webserver troubleshooting](../troubleshooting.md)

### OPDS continuation

The OPDS transport stages one complete server response on SD before invoking
the parser, so TLS and retained catalog entries do not coexist. Next reads
the staged response and starts at the first unseen entry. The reader follows the server's next-page link only
after finishing that page. Local page turns retain only start/end cursors.
Previous and Back first scan the saved response to reconstruct the preceding
range, then reset the parser and load that range. The passes run sequentially
without retaining an offset list or two parser backends.
Entry ranges count source entries, not facets or account shortcuts. The server
must keep its ordering stable between downloads. Only the active server page
is a snapshot; earlier server pages are downloaded again. Retry discards the
snapshot and resets the local range.

Only server/category transitions enter navigation history: at most 16 records
and an 8 KiB budget for vector/string storage and allocation overhead. Oldest
records are discarded first. At the first local range, Back returns home when
that history is exhausted;
Previous can continue via server-provided previous-page links, loading the last
local range of the older response. Long addresses/search queries can shorten
the retained history. An individual position exceeding the budget fails without
changing history.

Verify a working shelf and a large acquisition feed, then traverse local ranges
and server pages in both directions. Confirm no skipped or repeated book IDs and
record free heap and largest blocks during repeated navigation. Check page buttons,
touch pagination where available, and all four orientations. Confirm that local ranges make no HTTP request,
the staging file is removed on exit/failure, and a missing/full SD card reports
a storage error. Record internal free heap and largest blocks before downloading,
before replay, and after rendering; the host C++ allocator model does not account
for Expat C allocations or actual TLS buffers.

Regression checks must combine the production browser navigation methods with
the SDK client/parsers and allocation accounting. Cross 63/64/65 and 127/128/129
local turns, traverse at least 1300 entries both ways, and verify retained local
state stays constant. Exercise history eviction with short/long URLs and queries,
server pagination, Back/Home, search, Retry, and backward-scan failures.
