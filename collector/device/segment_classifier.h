/* Frozen mem_analyze-v6 WPS segment rules shared by CLI and library. */
#ifndef HC_SEGMENT_CLASSIFIER_H
#define HC_SEGMENT_CLASSIFIER_H
#include <stdbool.h>
#include <ctype.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#ifndef PATH_MAX
#define PATH_MAX 4096
#endif
typedef enum {
    SEG_ARK_TS_HEAP = 0,
    SEG_NATIVE_HEAP,
    SEG_ANON_PAGE_OTHER,
    SEG_FILE_PAGE_OTHER,
    SEG_STACK,
    SEG_SO,
    SEG_HAP,
    SEG_TTF,
    SEG_DEV,
    SEG_GL,
    SEG_GRAPH,
    SEG_GUARD,
    SEG_UNKNOWN,
    SEG_COUNT
} SegmentKind;

static const char *segment_name(SegmentKind s)
{
    switch (s) {
    case SEG_ARK_TS_HEAP: return "ark ts heap";
    case SEG_NATIVE_HEAP: return "native heap";
    case SEG_ANON_PAGE_OTHER: return "AnonPage other";
    case SEG_FILE_PAGE_OTHER: return "FilePage other";
    case SEG_STACK: return "stack";
    case SEG_SO: return ".so";
    case SEG_HAP: return ".hap";
    case SEG_TTF: return ".ttf";
    case SEG_DEV: return "dev";
    case SEG_GL: return "GL";
    case SEG_GRAPH: return "Graph";
    case SEG_GUARD: return "guard";
    default: return "unknown";
    }
}

static bool starts_with(const char *s, const char *prefix)
{
    return strncmp(s, prefix, strlen(prefix)) == 0;
}

static void clean_deleted_suffix(const char *in, char *out, size_t len)
{
    const char *suffix = " (deleted)";
    size_t n;
    snprintf(out, len, "%s", in);
    n = strlen(out);
    if (n >= strlen(suffix) && strcmp(out + n - strlen(suffix), suffix) == 0) {
        out[n - strlen(suffix)] = '\0';
    }
}

static bool ends_with(const char *s, const char *suffix)
{
    size_t s_len;
    size_t suffix_len;

    if (s == NULL || suffix == NULL) return false;
    s_len = strlen(s);
    suffix_len = strlen(suffix);
    if (suffix_len > s_len) return false;
    return strcmp(s + s_len - suffix_len, suffix) == 0;
}

static bool contains_ci(const char *haystack, const char *needle)
{
    size_t needle_len;

    if (haystack == NULL || needle == NULL) return false;
    needle_len = strlen(needle);
    if (needle_len == 0) return true;

    for (const char *p = haystack; *p != '\0'; p++) {
        size_t i;
        for (i = 0; i < needle_len; i++) {
            if (p[i] == '\0') return false;
            if (tolower((unsigned char)p[i]) != tolower((unsigned char)needle[i])) break;
        }
        if (i == needle_len) return true;
    }
    return false;
}

static bool has_file_suffix(const char *path, const char *suffix)
{
    char clean[PATH_MAX];
    clean_deleted_suffix(path, clean, sizeof(clean));
    return ends_with(clean, suffix);
}

static bool is_dev_path(const char *path)
{
    return starts_with(path, "/dev/") || starts_with(path, "anon_inode:");
}

static bool is_guard_path(const char *path, const char *perms)
{
    return contains_ci(path, "guard") ||
           strcmp(path, "[anon:thread signal stack guard page]") == 0 ||
           (perms != NULL && perms[0] == '-' && perms[1] == '-' && perms[2] == '-');
}

static bool is_graph_path(const char *path)
{
    return contains_ci(path, "graph") ||
           contains_ci(path, "graphic") ||
           contains_ci(path, "gralloc") ||
           contains_ci(path, "surface") ||
           contains_ci(path, "framebuffer") ||
           contains_ci(path, "frame_buffer") ||
           contains_ci(path, "bufferqueue") ||
           contains_ci(path, "sharedbuffer");
}

static bool is_gl_path(const char *path)
{
    return contains_ci(path, "gl") ||
           contains_ci(path, "egl") ||
           contains_ci(path, "gles") ||
           contains_ci(path, "gpu") ||
           contains_ci(path, "mali") ||
           contains_ci(path, "vulkan") ||
           contains_ci(path, "render");
}

static bool is_ark_ts_heap_path(const char *path)
{
    return contains_ci(path, "ark ts heap") ||
           contains_ci(path, "arkts heap") ||
           contains_ci(path, "ark heap") ||
           contains_ci(path, "ets heap") ||
           contains_ci(path, "js heap") ||
           (contains_ci(path, "ark") && contains_ci(path, "heap"));
}

static bool is_native_heap_path(const char *path)
{
    if (strcmp(path, "[heap]") == 0) return true;
    return contains_ci(path, "native heap") ||
           contains_ci(path, "malloc") ||
           contains_ci(path, "jemalloc") ||
           contains_ci(path, "scudo") ||
           contains_ci(path, "libc_malloc") ||
           contains_ci(path, "heap");
}


static SegmentKind hc_classify_wps(const char *path, const char *perms, uint64_t inode)
{
        bool is_anon = (inode == 0 && path[0] == '\0') || starts_with(path, "[anon:");

        if (is_guard_path(path, perms)) {
            return SEG_GUARD;
        } else if (starts_with(path, "[stack")) {
            return SEG_STACK;
        } else if (is_ark_ts_heap_path(path)) {
            return SEG_ARK_TS_HEAP;
        } else if (is_native_heap_path(path)) {
            return SEG_NATIVE_HEAP;
        } else if (is_dev_path(path)) {
            return SEG_DEV;
        } else if (is_gl_path(path)) {
            return SEG_GL;
        } else if (is_graph_path(path)) {
            return SEG_GRAPH;
        } else if (has_file_suffix(path, ".so")) {
            return SEG_SO;
        } else if (has_file_suffix(path, ".hap")) {
            return SEG_HAP;
        } else if (has_file_suffix(path, ".ttf") || has_file_suffix(path, ".otf")) {
            return SEG_TTF;
        } else if (is_anon) {
            return SEG_ANON_PAGE_OTHER;
        } else if (path[0] != '\0') {
            return SEG_FILE_PAGE_OTHER;
        } else {
            return SEG_UNKNOWN;
        }
}
#endif
