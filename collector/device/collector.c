#define _POSIX_C_SOURCE 200809L
#include "harmony_ops/collector.h"
#include "segment_classifier.h"

#include <errno.h>
#include <inttypes.h>
#include <limits.h>
#include <stdarg.h>
#include <stdlib.h>

#if defined(__linux__) || defined(__OHOS__)
#define HC_HAS_PROCFS 1
#include <fcntl.h>
#include <time.h>
#include <unistd.h>
#else
#define HC_HAS_PROCFS 0
#endif

static hc_status fail(hc_error *error, hc_status code, int system_errno,
                      int32_t pid, const char *format, ...)
{
    if (error) {
        va_list ap;
        memset(error, 0, sizeof(*error));
        error->code = code;
        error->system_errno = system_errno;
        error->pid = pid;
        va_start(ap, format);
        vsnprintf(error->message, sizeof(error->message), format, ap);
        va_end(ap);
    }
    return code;
}

static void clear_error(hc_error *error)
{
    if (error) memset(error, 0, sizeof(*error));
}

const char *hc_status_name(hc_status status)
{
    switch (status) {
    case HC_OK: return "ok";
    case HC_INVALID_ARGUMENT: return "invalid_argument";
    case HC_UNSUPPORTED: return "unsupported";
    case HC_PERMISSION_DENIED: return "permission_denied";
    case HC_IO_ERROR: return "io_error";
    case HC_PARSE_ERROR: return "parse_error";
    case HC_MISSING_REFERENCED: return "missing_referenced";
    case HC_EMPTY_SNAPSHOT: return "empty_snapshot";
    case HC_PROCESS_CHANGED: return "process_changed";
    case HC_NO_MEMORY: return "no_memory";
    default: return "unknown_error";
    }
}

void hc_snapshot_free(hc_snapshot *snapshot)
{
    if (snapshot) {
        free(snapshot->vmas);
        memset(snapshot, 0, sizeof(*snapshot));
    }
}

static int valid_page_size(uint64_t value)
{
    return value >= 1024 && value <= UINT64_C(1073741824) &&
           (value & (value - 1)) == 0;
}

static char *trim(char *text)
{
    char *end;
    while (*text && isspace((unsigned char)*text)) ++text;
    end = text + strlen(text);
    while (end > text && isspace((unsigned char)end[-1])) --end;
    *end = '\0';
    return text;
}

static int same_key(const char *a, const char *b)
{
    while (*a && *b) {
        if (tolower((unsigned char)*a++) != tolower((unsigned char)*b++)) return 0;
    }
    return *a == *b;
}

static int numeric_token(const char **cursor, int base, uint64_t *value)
{
    char *end;
    unsigned long long parsed;
    const char *text = *cursor;
    while (*text == ' ' || *text == '\t') ++text;
    if (!(base == 16 ? isxdigit((unsigned char)*text) : isdigit((unsigned char)*text))) return 0;
    errno = 0;
    parsed = strtoull(text, &end, base);
    if (errno == ERANGE || end == text) return 0;
    *value = (uint64_t)parsed;
    *cursor = end;
    return 1;
}

static int word_token(const char **cursor, char *out, size_t capacity)
{
    const char *text = *cursor, *end;
    size_t length;
    if (*text != ' ' && *text != '\t') return 0;
    while (*text == ' ' || *text == '\t') ++text;
    end = text;
    while (*end && *end != ' ' && *end != '\t') ++end;
    length = (size_t)(end - text);
    if (!length || length >= capacity) return 0;
    memcpy(out, text, length);
    out[length] = '\0';
    *cursor = end;
    return 1;
}

/* 1: valid header, 0: a field, -1: malformed/truncated header. */
static int parse_header(const char *line, hc_vma *vma)
{
    uint64_t start, end, offset, inode, major, minor;
    char perms[8], device[32];
    const char *cursor = line, *dev_cursor;
    const char *dash = strchr(line, '-');
    const char *space = strpbrk(line, " \t");
    if (!dash || (space && dash > space)) return 0;
    if (!numeric_token(&cursor, 16, &start) || *cursor++ != '-' ||
        !numeric_token(&cursor, 16, &end) ||
        !word_token(&cursor, perms, sizeof(perms)) ||
        (*cursor != ' ' && *cursor != '\t') ||
        !numeric_token(&cursor, 16, &offset) ||
        !word_token(&cursor, device, sizeof(device)) ||
        (*cursor != ' ' && *cursor != '\t') ||
        !numeric_token(&cursor, 10, &inode) ||
        (*cursor && *cursor != ' ' && *cursor != '\t')) return -1;
    if (end <= start || strlen(perms) != 4 ||
        (perms[0] != 'r' && perms[0] != '-') ||
        (perms[1] != 'w' && perms[1] != '-') ||
        (perms[2] != 'x' && perms[2] != '-') ||
        (perms[3] != 'p' && perms[3] != 's')) return -1;
    dev_cursor = device;
    if (!numeric_token(&dev_cursor, 16, &major) || *dev_cursor++ != ':' ||
        !numeric_token(&dev_cursor, 16, &minor) || *dev_cursor) return -1;
    while (*cursor == ' ' || *cursor == '\t') ++cursor;
    if (strlen(cursor) >= sizeof(vma->pathname)) return -1;
    memset(vma, 0, sizeof(*vma));
    vma->start = start;
    vma->end = end;
    vma->offset = offset;
    vma->inode = inode;
    strcpy(vma->permissions, perms);
    strcpy(vma->device, device);
    strcpy(vma->pathname, cursor);
    return 1;
}

static hc_status finish_vma(hc_snapshot *snapshot, size_t *capacity,
                            hc_vma *vma, int have_referenced,
                            hc_segment_mode mode, hc_error *error)
{
    hc_vma *items;
    if (!have_referenced)
        return fail(error, HC_MISSING_REFERENCED, 0, 0,
                    "VMA at 0x%" PRIx64 " has no Referenced field", vma->start);
    if (snapshot->count && vma->start < snapshot->vmas[snapshot->count - 1].end)
        return fail(error, HC_PARSE_ERROR, 0, 0, "VMA address ranges overlap or are unordered");
    if (vma->referenced_bytes > vma->end - vma->start)
        return fail(error, HC_PARSE_ERROR, 0, 0, "Referenced exceeds VMA address range");
    vma->referenced_pages = vma->referenced_bytes / snapshot->page_size +
        (vma->referenced_bytes % snapshot->page_size != 0);
    if (mode == HC_SEGMENT_WPS) {
        strcpy(vma->segment, segment_name(hc_classify_wps(vma->pathname,
                                                       vma->permissions, vma->inode)));
    } else if (!vma->segment[0]) {
        strcpy(vma->segment, vma->pathname[0] == '/' ? "FilePage other" : "AnonPage other");
    }
    if (snapshot->count == *capacity) {
        size_t next = *capacity ? *capacity * 2 : 64;
        if (next < *capacity || next > SIZE_MAX / sizeof(*items))
            return fail(error, HC_NO_MEMORY, 0, 0, "too many VMA rows");
        items = (hc_vma *)realloc(snapshot->vmas, next * sizeof(*items));
        if (!items) return fail(error, HC_NO_MEMORY, errno, 0, "cannot allocate VMA rows");
        snapshot->vmas = items;
        *capacity = next;
    }
    snapshot->vmas[snapshot->count++] = *vma;
    return HC_OK;
}

hc_status hc_parse_smaps(const char *text, size_t length, uint64_t page_size,
                         hc_segment_mode mode, hc_snapshot *out, hc_error *error)
{
    hc_snapshot result = {0};
    hc_vma current = {0};
    size_t capacity = 0, position = 0, line_number = 0;
    int have_current = 0, have_referenced = 0;
    hc_status status = HC_OK;
    clear_error(error);
    if (!out || out->vmas || out->count || !text || !valid_page_size(page_size) ||
        (mode != HC_SEGMENT_WPS && mode != HC_SEGMENT_CROSS_APP))
        return fail(error, HC_INVALID_ARGUMENT, 0, 0, "invalid parser argument or nonempty output");
    memset(out, 0, sizeof(*out));
    if (!length) return fail(error, HC_EMPTY_SNAPSHOT, 0, 0, "empty smaps text");
    if (text[length - 1] != '\n' || memchr(text, '\0', length))
        return fail(error, HC_PARSE_ERROR, 0, 0, "smaps text is truncated or contains NUL");
    result.page_size = page_size;
    while (position < length) {
        char buffer[8192];
        const char *newline = (const char *)memchr(text + position, '\n', length - position);
        size_t line_length = (size_t)(newline - text - position);
        char *line, *colon;
        hc_vma candidate;
        int header;
        ++line_number;
        if (line_length >= sizeof(buffer)) {
            status = fail(error, HC_PARSE_ERROR, 0, 0, "smaps line %zu exceeds maximum length", line_number);
            goto failed;
        }
        memcpy(buffer, text + position, line_length);
        buffer[line_length] = '\0';
        position += line_length + 1;
        line = trim(buffer);
        if (!*line) continue;
        header = parse_header(line, &candidate);
        if (header < 0) {
            status = fail(error, HC_PARSE_ERROR, 0, 0, "malformed VMA header at line %zu", line_number);
            goto failed;
        }
        if (header) {
            if (have_current) {
                status = finish_vma(&result, &capacity, &current, have_referenced, mode, error);
                if (status != HC_OK) goto failed;
            }
            current = candidate;
            have_current = 1;
            have_referenced = 0;
            continue;
        }
        colon = strchr(line, ':');
        if (!have_current || !colon) {
            status = fail(error, HC_PARSE_ERROR, 0, 0, "unexpected smaps text at line %zu", line_number);
            goto failed;
        }
        *colon = '\0';
        if (same_key(trim(line), "Referenced")) {
            char *end, *value = trim(colon + 1);
            unsigned long long kb;
            errno = 0;
            if (have_referenced || !isdigit((unsigned char)*value)) {
                status = fail(error, HC_PARSE_ERROR, 0, 0, "invalid or duplicate Referenced at line %zu", line_number);
                goto failed;
            }
            kb = strtoull(value, &end, 10);
            end = trim(end);
            if (errno == ERANGE || strcmp(end, "kB") || kb > UINT64_MAX / 1024) {
                status = fail(error, HC_PARSE_ERROR, 0, 0, "invalid Referenced unit/value at line %zu", line_number);
                goto failed;
            }
            current.referenced_bytes = (uint64_t)kb * 1024;
            have_referenced = 1;
        } else if (mode == HC_SEGMENT_CROSS_APP &&
                   (same_key(trim(line), "Vma_segment") || same_key(trim(line), "Segment"))) {
            char *value = trim(colon + 1);
            if (!*value || strlen(value) >= sizeof(current.segment)) {
                status = fail(error, HC_PARSE_ERROR, 0, 0, "invalid explicit VMA segment at line %zu", line_number);
                goto failed;
            }
            strcpy(current.segment, value);
        }
    }
    if (have_current) {
        status = finish_vma(&result, &capacity, &current, have_referenced, mode, error);
        if (status != HC_OK) goto failed;
    }
    if (!result.count) {
        status = fail(error, HC_EMPTY_SNAPSHOT, 0, 0, "smaps contains no VMA rows");
        goto failed;
    }
    *out = result;
    return HC_OK;
failed:
    hc_snapshot_free(&result);
    return status;
}

#if HC_HAS_PROCFS
static hc_status os_error(hc_error *error, int32_t pid, const char *what)
{
    int number = errno;
    hc_status code = number == EACCES || number == EPERM ? HC_PERMISSION_DENIED :
        (number == ENOENT || number == ESRCH ? HC_PROCESS_CHANGED : HC_IO_ERROR);
    return fail(error, code, number, pid, "%s: %s", what, strerror(number));
}

static hc_status open_process(int32_t pid, int *out, hc_error *error)
{
    char path[64];
    if (pid <= 0) return fail(error, HC_INVALID_ARGUMENT, 0, pid, "PID must be positive");
    snprintf(path, sizeof(path), "/proc/%" PRId32, pid);
    *out = open(path, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (*out < 0) return os_error(error, pid, "cannot open process directory");
    return HC_OK;
}

static hc_status read_fd(int fd, int32_t pid, char **out, size_t *length, hc_error *error)
{
    size_t used = 0, capacity = 16384;
    char *buffer = (char *)malloc(capacity + 1);
    if (!buffer) return fail(error, HC_NO_MEMORY, errno, pid, "cannot allocate procfs buffer");
    for (;;) {
        ssize_t count;
        if (used == capacity) {
            char *next;
            if (capacity >= 256 * 1024 * 1024) {
                free(buffer);
                return fail(error, HC_IO_ERROR, 0, pid, "procfs input exceeds 256 MiB limit");
            }
            capacity *= 2;
            next = (char *)realloc(buffer, capacity + 1);
            if (!next) { free(buffer); return fail(error, HC_NO_MEMORY, errno, pid, "cannot grow procfs buffer"); }
            buffer = next;
        }
        count = read(fd, buffer + used, capacity - used);
        if (count < 0 && errno == EINTR) continue;
        if (count < 0) { hc_status status = os_error(error, pid, "cannot read procfs file"); free(buffer); return status; }
        if (!count) break;
        used += (size_t)count;
    }
    buffer[used] = '\0';
    *out = buffer;
    *length = used;
    return HC_OK;
}

static hc_status read_at(int directory, const char *name, int32_t pid,
                         char **out, size_t *length, hc_error *error)
{
    int fd = openat(directory, name, O_RDONLY | O_CLOEXEC);
    hc_status status;
    if (fd < 0) return os_error(error, pid, "cannot open procfs file");
    status = read_fd(fd, pid, out, length, error);
    if (close(fd) != 0 && status == HC_OK) status = os_error(error, pid, "cannot close procfs file");
    return status;
}

static hc_status identity_at(int directory, int32_t pid,
                             hc_process_identity *out, hc_error *error)
{
    char *text = NULL, *tail, *token, *end;
    size_t length = 0;
    unsigned int field;
    uint64_t start = 0;
    hc_status status = read_at(directory, "stat", pid, &text, &length, error);
    if (status != HC_OK) { free(text); return status; }
    tail = strrchr(text, ')');
    if (!length || !tail || tail[1] != ' ' || !tail[2] || tail[3] != ' ') {
        free(text); return fail(error, HC_PARSE_ERROR, 0, pid, "invalid process stat identity");
    }
    if (tail[2] == 'Z' || tail[2] == 'X' || tail[2] == 'x') {
        free(text); return fail(error, HC_PROCESS_CHANGED, 0, pid, "process is zombie/dead");
    }
    token = tail + 2;
    for (field = 3; field <= 22; ++field) {
        while (*token == ' ') ++token;
        if (!*token || *token == '\n') { free(text); return fail(error, HC_PARSE_ERROR, 0, pid, "process stat lacks starttime"); }
        end = token;
        while (*end && *end != ' ' && *end != '\n') ++end;
        if (field == 22) {
            char saved = *end;
            char *parse_end;
            *end = '\0';
            errno = 0;
            if (!isdigit((unsigned char)*token)) { free(text); return fail(error, HC_PARSE_ERROR, 0, pid, "invalid process starttime"); }
            start = (uint64_t)strtoull(token, &parse_end, 10);
            if (errno == ERANGE || *parse_end) { free(text); return fail(error, HC_PARSE_ERROR, 0, pid, "invalid process starttime"); }
            *end = saved;
        }
        token = end;
    }
    free(text);
    out->pid = pid;
    out->start_ticks = start;
    return HC_OK;
}

static hc_status verify_at(int directory, const hc_process_identity *expected, hc_error *error)
{
    hc_process_identity actual;
    hc_status status = identity_at(directory, expected->pid, &actual, error);
    if (status != HC_OK) return status;
    if (actual.start_ticks != expected->start_ticks)
        return fail(error, HC_PROCESS_CHANGED, 0, expected->pid, "process starttime changed");
    return HC_OK;
}

static hc_status monotonic_now(uint64_t *out, int32_t pid, hc_error *error)
{
    struct timespec stamp;
    if (clock_gettime(CLOCK_MONOTONIC, &stamp)) return os_error(error, pid, "monotonic clock failed");
    *out = (uint64_t)stamp.tv_sec * UINT64_C(1000000000) + (uint64_t)stamp.tv_nsec;
    return HC_OK;
}
#endif

hc_status hc_read_identity(int32_t pid, hc_process_identity *out, hc_error *error)
{
    clear_error(error);
    if (!out || pid <= 0) return fail(error, HC_INVALID_ARGUMENT, 0, pid, "invalid identity argument");
    memset(out, 0, sizeof(*out));
#if HC_HAS_PROCFS
    {
        int directory;
        hc_status status = open_process(pid, &directory, error);
        if (status != HC_OK) return status;
        status = identity_at(directory, pid, out, error);
        close(directory);
        return status;
    }
#else
    return fail(error, HC_UNSUPPORTED, 0, pid, "procfs identity requires Linux/OpenHarmony");
#endif
}

hc_status hc_collect(const hc_process_identity *expected, hc_segment_mode mode,
                     hc_snapshot *out, hc_error *error)
{
    clear_error(error);
    if (!expected || expected->pid <= 0 || !out || out->vmas || out->count ||
        (mode != HC_SEGMENT_WPS && mode != HC_SEGMENT_CROSS_APP))
        return fail(error, HC_INVALID_ARGUMENT, 0, expected ? expected->pid : 0, "invalid collection argument");
    memset(out, 0, sizeof(*out));
#if HC_HAS_PROCFS
    {
        int directory;
        char *text = NULL;
        size_t length = 0;
        uint64_t started = 0, finished = 0;
        long page_size;
        hc_status status = open_process(expected->pid, &directory, error);
        if (status != HC_OK) return status;
        status = monotonic_now(&started, expected->pid, error);
        if (status != HC_OK) goto done;
        status = verify_at(directory, expected, error);
        if (status != HC_OK) goto done;
        errno = 0;
        page_size = sysconf(_SC_PAGESIZE);
        if (page_size <= 0 || !valid_page_size((uint64_t)page_size)) {
            status = fail(error, HC_IO_ERROR, errno, expected->pid, "cannot determine actual page size");
            goto done;
        }
        status = read_at(directory, "smaps", expected->pid, &text, &length, error);
        if (status != HC_OK) goto done;
        status = verify_at(directory, expected, error);
        if (status != HC_OK) goto done;
        status = hc_parse_smaps(text, length, (uint64_t)page_size, mode, out, error);
        if (status != HC_OK) { if (error) error->pid = expected->pid; goto done; }
        status = monotonic_now(&finished, expected->pid, error);
        if (status != HC_OK) { hc_snapshot_free(out); goto done; }
        out->identity = *expected;
        out->started_ns = started;
        out->finished_ns = finished;
    done:
        free(text);
        close(directory);
        return status;
    }
#else
    return fail(error, HC_UNSUPPORTED, 0, expected->pid, "procfs collection requires Linux/OpenHarmony");
#endif
}

hc_status hc_clear_refs(const hc_process_identity *expected, hc_error *error)
{
    clear_error(error);
    if (!expected || expected->pid <= 0) return fail(error, HC_INVALID_ARGUMENT, 0, 0, "invalid clear_refs argument");
#if HC_HAS_PROCFS
    {
        int directory, fd = -1;
        ssize_t written;
        hc_status status = open_process(expected->pid, &directory, error);
        if (status != HC_OK) return status;
        status = verify_at(directory, expected, error);
        if (status != HC_OK) goto done;
        fd = openat(directory, "clear_refs", O_WRONLY | O_CLOEXEC);
        if (fd < 0) { status = os_error(error, expected->pid, "cannot open clear_refs"); goto done; }
        do { written = write(fd, "1\n", 2); } while (written < 0 && errno == EINTR);
        if (written != 2) {
            status = written < 0 ? os_error(error, expected->pid, "cannot write clear_refs") :
                fail(error, HC_IO_ERROR, 0, expected->pid, "short write to clear_refs");
            goto done;
        }
        if (close(fd)) { fd = -1; status = os_error(error, expected->pid, "cannot close clear_refs"); goto done; }
        fd = -1;
        status = verify_at(directory, expected, error);
    done:
        if (fd >= 0) close(fd);
        close(directory);
        return status;
    }
#else
    return fail(error, HC_UNSUPPORTED, 0, expected->pid, "clear_refs requires Linux/OpenHarmony");
#endif
}
