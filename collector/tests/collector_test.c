#include "harmony_ops/collector.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#if defined(__linux__) || defined(__OHOS__)
#include <unistd.h>
#endif

#define CHECK(condition) do { if (!(condition)) { \
    fprintf(stderr, "collector check failed at line %d: %s\n", __LINE__, #condition); return 1; \
} } while (0)

static int expect_error(const char *text, hc_status expected)
{
    hc_snapshot snapshot = {0};
    hc_error error = {0};
    hc_status status = hc_parse_smaps(text, strlen(text), 4096, HC_SEGMENT_WPS, &snapshot, &error);
    CHECK(status == expected);
    CHECK(error.code == expected);
    CHECK(error.message[0]);
    CHECK(!snapshot.count && !snapshot.vmas);
    return 0;
}

int main(void)
{
    const char *fixture =
        "1000-5000 rw-p 00000000 00:00 0 [heap]\n"
        "Size: 16 kB\nRss: 12 kB\nReferenced: 12 kB\nVmFlags: rd wr mr mw me ac sd\n"
        "5000-9000 r-xp 00000000 08:01 14 /usr/lib/libsample.so (deleted)\n"
        "Referenced: 4 kB\n"
        "9000-d000 ---p 00000000 00:00 0\nReferenced: 0 kB\n"
        "d000-11000 rw-p 00000000 00:00 0\nReferenced: 4 kB\n";
    hc_snapshot snapshot = {0};
    hc_error error = {0};
    CHECK(hc_parse_smaps(fixture, strlen(fixture), 16384, HC_SEGMENT_WPS, &snapshot, &error) == HC_OK);
    CHECK(snapshot.page_size == 16384 && snapshot.count == 4);
    CHECK(snapshot.vmas[0].referenced_bytes == 12288 && snapshot.vmas[0].referenced_pages == 1);
    CHECK(!strcmp(snapshot.vmas[0].segment, "native heap"));
    CHECK(!strcmp(snapshot.vmas[1].segment, ".so"));
    CHECK(!strcmp(snapshot.vmas[2].segment, "guard"));
    CHECK(!strcmp(snapshot.vmas[3].segment, "AnonPage other"));
    CHECK(snapshot.started_ns == 0 && snapshot.identity.pid == 0);
    /* Ownership is explicit: parsing into an occupied output is rejected. */
    CHECK(hc_parse_smaps(fixture, strlen(fixture), 4096, HC_SEGMENT_WPS, &snapshot, &error) == HC_INVALID_ARGUMENT);
    hc_snapshot_free(&snapshot);
    hc_snapshot_free(&snapshot);
    CHECK(hc_parse_smaps(fixture, strlen(fixture), 4096, HC_SEGMENT_CROSS_APP, &snapshot, &error) == HC_OK);
    CHECK(!strcmp(snapshot.vmas[0].segment, "AnonPage other"));
    CHECK(!strcmp(snapshot.vmas[1].segment, "FilePage other"));
    CHECK(snapshot.vmas[0].referenced_pages == 3);
    hc_snapshot_free(&snapshot);
    {
        const char *explicit_segment = "1000-5000 rw-p 0 00:00 0 /a b\nReferenced: 4 kB\nVma_segment: custom file\n";
        CHECK(hc_parse_smaps(explicit_segment, strlen(explicit_segment), 8192, HC_SEGMENT_CROSS_APP, &snapshot, &error) == HC_OK);
        CHECK(!strcmp(snapshot.vmas[0].pathname, "/a b"));
        CHECK(!strcmp(snapshot.vmas[0].segment, "custom file"));
        CHECK(snapshot.vmas[0].referenced_bytes == 4096 && snapshot.vmas[0].referenced_pages == 1);
        hc_snapshot_free(&snapshot);
    }
    CHECK(expect_error("", HC_EMPTY_SNAPSHOT) == 0);
    CHECK(expect_error("\n", HC_EMPTY_SNAPSHOT) == 0);
    CHECK(expect_error("1000-5000 rw-p 0 00:00 0\nRss: 4 kB\n", HC_MISSING_REFERENCED) == 0);
    CHECK(expect_error("1000-5000 rw-p 0 00:00 0\nReferenced: 4 kB", HC_PARSE_ERROR) == 0);
    CHECK(expect_error("1000-5000 rw-p 0 00:00 0\nReferenced: -1 kB\n", HC_PARSE_ERROR) == 0);
    CHECK(expect_error("1000-5000 rw-p 0 00:00 0\nReferenced: 4 bytes\n", HC_PARSE_ERROR) == 0);
    CHECK(expect_error("1000-5000 rw-p 0 00:00 0\nReferenced: 4 kB\nReferenced: 4 kB\n", HC_PARSE_ERROR) == 0);
    CHECK(expect_error("1000-5000 rw-p 0 00:00 0\nReferenced: 100 kB\n", HC_PARSE_ERROR) == 0);
    CHECK(expect_error("1000-1000 rw-p 0 00:00 0\nReferenced: 0 kB\n", HC_PARSE_ERROR) == 0);
    CHECK(expect_error("1000-5000 rw-p 0 00:00 -1\nReferenced: 0 kB\n", HC_PARSE_ERROR) == 0);
    CHECK(expect_error("fffffffffffffffff-5000 rw-p 0 00:00 0\nReferenced: 0 kB\n", HC_PARSE_ERROR) == 0);
    CHECK(expect_error("1000-5000 rw-p 0 00:00 0\nReferenced: 4 kB\n4000-6000 r--p 0 00:00 0\nReferenced: 0 kB\n", HC_PARSE_ERROR) == 0);
    CHECK(expect_error("not smaps\n", HC_PARSE_ERROR) == 0);
    CHECK(hc_parse_smaps(fixture, strlen(fixture), 0, HC_SEGMENT_WPS, &snapshot, &error) == HC_INVALID_ARGUMENT);
    CHECK(hc_parse_smaps(fixture, strlen(fixture), 6000, HC_SEGMENT_WPS, &snapshot, &error) == HC_INVALID_ARGUMENT);
    CHECK(hc_read_identity(-1, &snapshot.identity, &error) == HC_INVALID_ARGUMENT);
#if defined(__linux__) || defined(__OHOS__)
    {
        hc_process_identity self, changed;
        CHECK(hc_read_identity((int32_t)getpid(), &self, &error) == HC_OK);
        CHECK(self.pid == getpid());
        CHECK(hc_collect(&self, HC_SEGMENT_WPS, &snapshot, &error) == HC_OK);
        CHECK(snapshot.count > 0 && snapshot.identity.start_ticks == self.start_ticks);
        CHECK(snapshot.started_ns > 0 && snapshot.finished_ns >= snapshot.started_ns);
        CHECK(snapshot.page_size == (uint64_t)sysconf(_SC_PAGESIZE));
        hc_snapshot_free(&snapshot);
        changed = self;
        ++changed.start_ticks;
        CHECK(hc_collect(&changed, HC_SEGMENT_WPS, &snapshot, &error) == HC_PROCESS_CHANGED);
        CHECK(!snapshot.count && !snapshot.vmas);
        CHECK(hc_clear_refs(&changed, &error) == HC_PROCESS_CHANGED);
        /* This test owns its own process; no third-party target is modified. */
        CHECK(hc_clear_refs(&self, &error) == HC_OK);
        CHECK(hc_collect(&self, HC_SEGMENT_CROSS_APP, &snapshot, &error) == HC_OK);
        hc_snapshot_free(&snapshot);
    }
#else
    {
        hc_process_identity fake = {1, 1};
        CHECK(hc_read_identity(1, &fake, &error) == HC_UNSUPPORTED);
        fake.pid = 1;
        CHECK(hc_collect(&fake, HC_SEGMENT_WPS, &snapshot, &error) == HC_UNSUPPORTED);
    }
#endif
    puts("collector parser, ownership and process checks passed");
    return 0;
}
