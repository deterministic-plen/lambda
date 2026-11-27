"""Small C runtime extensions that are not part of RUNTIME_C.

`runtime.py` is the core runtime.  Additions that are independent of the core
allocator live here and are concatenated to it before compilation, so the
core file stays reviewable and mergeable.
"""

RUNTIME_EXTRA_C = r"""
#include <time.h>
#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#endif

/* ------------------------------------------------------------------ */
/* Boxed product values (Array<T> element support for sums/records)    */
/* ------------------------------------------------------------------ */
/* A product/sum value is an inline tagged struct; to store it in a
   generic Array<T> it is copied byte-for-byte into an ArrayB.  The bytes
   contain only i64/double words (no pointers), so a shallow copy is a full
   value copy and the ArrayB reference-counting/free path is reused. */
Value *lambda_box_record_bytes(const void *data, int64_t n) {
    if (n < 0) fail("product size must be non-negative");
    Value *v = lambda_arrayb_new(n, 0);
    if (n > 0) {
        memcpy(v->p, data, (size_t)n);
    }
    return v;
}

void *lambda_record_bytes(Value *v) {
    if (!v || v->tag != TAG_ARRAYB) {
        fail_type("product", v);
    }
    return v->p;
}


/* ------------------------------------------------------------------ */
/* IO: read-file as a tagged IOResult value                            */
/* ------------------------------------------------------------------ */
/* Layout matches the boxed data declaration
     data IOResult = Ok (value: String) | Err (code: Int)
   i.e. [tag, payload].  `read_regular_file` returns NULL for directories,
   unseekable files and open/read errors; empty regular files succeed. */
Value *lambda_read_file_io(Value *pathv) {
    if (!pathv || pathv->tag != TAG_STRING) fail_type("String", pathv);
    const char *path = (const char *)pathv->p;
    if (memchr(path, 0, (size_t)pathv->i) != NULL) {
        lambda_release(pathv);
        return lambda_cons(lambda_box_int(1), lambda_cons(lambda_box_int(1), lambda_nil()));
    }
    size_t len = 0;
    char *buf = read_regular_file(path, &len);
    lambda_release(pathv);
    if (!buf) {
        return lambda_cons(lambda_box_int(1), lambda_cons(lambda_box_int(1), lambda_nil()));
    }
    Value *content = lambda_string_new_len(buf, (int64_t)len);
    free(buf);
    return lambda_cons(lambda_box_int(0), lambda_cons(content, lambda_nil()));
}

/* ------------------------------------------------------------------ */
/* State-cycle debugger / profiler                                     */
/* ------------------------------------------------------------------ */
/* The compiled program need not keep its last state: `(cycle state)` is an
   identity function that reports the state when LAMBDA_TRACE=1 and counts
   cycles for the end-of-run profile.  A short-lived fixed buffer keeps the
   debug path allocation-free and honest about reference counts. */
static int64_t g_cycle_count = 0;
static clock_t g_cycle_start = (clock_t)-1;

Value *lambda_cycle(Value *v) {
    if (g_cycle_count == 0) {
        g_cycle_start = clock();
    }
    g_cycle_count++;
    if (getenv("LAMBDA_TRACE")) {
        char buf[64];
        snprintf(buf, sizeof(buf), "cycle %lld: ", (long long)g_cycle_count);
        out_puts(buf);
        print_value_inner(v, 1);
        out_ch('\n');
        out_flush();
    }
    return v;
}

void lambda_profile_report(void) {
    if (!getenv("LAMBDA_PROFILE") || g_cycle_count == 0) {
        return;
    }
    double elapsed = 0.0;
    if (g_cycle_start != (clock_t)-1) {
        elapsed = (double)(clock() - g_cycle_start) / (double)CLOCKS_PER_SEC;
    }
    char buf[128];
    snprintf(buf, sizeof(buf),
             "lambda-profile: cycles=%lld cpu=%.3fs\n",
             (long long)g_cycle_count, elapsed);
    out_flush();
    fputs(buf, stderr);
    fflush(stderr);
}

/* ------------------------------------------------------------------ */
/* Native stack-overflow diagnostic                                    */
/* ------------------------------------------------------------------ */
#ifndef _WIN32
#include <signal.h>
#include <unistd.h>
static void lambda_stack_overflow(int sig) {
    (void)sig;
    const char msg[] = "lambda: stack overflow (recursion too deep)\n";
    ssize_t ignored = write(2, msg, sizeof(msg) - 1);
    (void)ignored;
    _exit(70);
}
void lambda_stack_guard(void) {
    static char altstack[65536];
    stack_t ss;
    ss.ss_sp = altstack;
    ss.ss_size = sizeof(altstack);
    ss.ss_flags = 0;
    if (sigaltstack(&ss, NULL) != 0) return;
    struct sigaction sa;
    memset(&sa, 0, sizeof(sa));
    sa.sa_handler = lambda_stack_overflow;
    sa.sa_flags = SA_ONSTACK;
    sigemptyset(&sa.sa_mask);
    sigaction(SIGSEGV, &sa, NULL);
    sigaction(SIGBUS, &sa, NULL);
}
#else
void lambda_stack_guard(void) {
    /* Windows reports stack exhaustion as an SEH exception; the default
       handler already terminates with a clear code.  A portable message
       would need a vectored exception handler, which is out of scope here. */
}
#endif

/* ------------------------------------------------------------------ */
/* GUI: framebuffer + native window (Win32) or PPM image fallback      */
/* ------------------------------------------------------------------ */
/* GUI is the data-backed product
       type GUI = (width: Int, height: Int, pixels: ArrayB)
   whose boxed layout is the cons list [tag, width, height, pixels].
   Pixels are B,G,R triples, width*height of them. */
static Value *gui_cons_head(Value *v) {
    if (!v || v->tag != TAG_CONS) fail("GUI: malformed value");
    return (Value *)(intptr_t)v->i;
}
static Value *gui_cons_tail(Value *v) {
    if (!v || v->tag != TAG_CONS) fail("GUI: malformed value");
    return (Value *)v->p;
}

#ifdef _WIN32
static unsigned char *g_gui_pixels;
static int g_gui_w, g_gui_h;

static LRESULT CALLBACK lambda_gui_wndproc(HWND hwnd, UINT msg,
                                           WPARAM wp, LPARAM lp) {
    switch (msg) {
    case WM_PAINT: {
        PAINTSTRUCT ps;
        HDC dc = BeginPaint(hwnd, &ps);
        BITMAPINFO bi;
        memset(&bi, 0, sizeof(bi));
        bi.bmiHeader.biSize = sizeof(BITMAPINFOHEADER);
        bi.bmiHeader.biWidth = g_gui_w;
        bi.bmiHeader.biHeight = -g_gui_h;  /* top-down */
        bi.bmiHeader.biPlanes = 1;
        bi.bmiHeader.biBitCount = 24;
        bi.bmiHeader.biCompression = BI_RGB;
        StretchDIBits(dc, 0, 0, g_gui_w, g_gui_h, 0, 0, g_gui_w, g_gui_h,
                      g_gui_pixels, &bi, DIB_RGB_COLORS, SRCCOPY);
        EndPaint(hwnd, &ps);
        return 0;
    }
    case WM_KEYDOWN:
        if (wp == VK_ESCAPE) PostQuitMessage(0);
        return 0;
    case WM_DESTROY:
        PostQuitMessage(0);
        return 0;
    default:
        (void)wp; (void)lp;
        return DefWindowProcA(hwnd, msg, wp, lp);
    }
}
#endif

void lambda_gui_show(Value *gui) {
    Value *tagv = gui_cons_head(gui);
    Value *rest = gui_cons_tail(gui);
    Value *wv = gui_cons_head(rest);
    rest = gui_cons_tail(rest);
    Value *hv = gui_cons_head(rest);
    rest = gui_cons_tail(rest);
    Value *pxv = gui_cons_head(rest);
    if (tagv->tag != TAG_INT || wv->tag != TAG_INT || hv->tag != TAG_INT
            || pxv->tag != TAG_ARRAYB) {
        fail("GUI: expected (GUI width height pixels) with an ArrayB buffer");
    }
    int64_t w = wv->i, h = hv->i;
    if (w <= 0 || h <= 0) fail("GUI: width and height must be positive");
    if (pxv->i != w * h * 3) fail("GUI: pixel buffer must be width*height*3 bytes");

    const char *headless = getenv("LAMBDA_GUI_HEADLESS");
#ifdef _WIN32
    if (!headless) {
        g_gui_pixels = (unsigned char *)pxv->p;
        g_gui_w = (int)w;
        g_gui_h = (int)h;
        WNDCLASSA wc;
        memset(&wc, 0, sizeof(wc));
        wc.lpfnWndProc = lambda_gui_wndproc;
        wc.hInstance = GetModuleHandleA(NULL);
        wc.hCursor = LoadCursor(NULL, IDC_ARROW);
        wc.hbrBackground = (HBRUSH)(COLOR_WINDOW + 1);
        wc.lpszClassName = "LambdaGUIWindow";
        if (!RegisterClassA(&wc) && GetLastError() != ERROR_CLASS_ALREADY_EXISTS) {
            fail("GUI: could not register window class");
        }
        RECT r = {0, 0, (LONG)w, (LONG)h};
        AdjustWindowRect(&r, WS_OVERLAPPEDWINDOW, FALSE);
        HWND hwnd = CreateWindowExA(0, "LambdaGUIWindow", "Lambda",
                                    WS_OVERLAPPEDWINDOW, CW_USEDEFAULT,
                                    CW_USEDEFAULT, r.right - r.left,
                                    r.bottom - r.top, NULL, NULL,
                                    GetModuleHandleA(NULL), NULL);
        if (!hwnd) fail("GUI: could not create window");
        ShowWindow(hwnd, SW_SHOW);
        UpdateWindow(hwnd);
        MSG msg;
        while (GetMessageA(&msg, NULL, 0, 0) > 0) {
            TranslateMessage(&msg);
            DispatchMessageA(&msg);
        }
        return;
    }
#else
    (void)headless;
#endif
    /* Headless / non-Windows: write an ordinary binary PPM (P6) so a GUI
       program remains testable and usable from scripts without a display. */
    const char *out = getenv("LAMBDA_GUI_OUT");
    if (!out || !*out) out = "gui.ppm";
    FILE *f = fopen(out, "wb");
    if (!f) fail("GUI: cannot open output PPM");
    fprintf(f, "P6\n%lld %lld\n255\n", (long long)w, (long long)h);
    unsigned char *p = (unsigned char *)pxv->p;
    for (int64_t i = 0; i < w * h; i++) {
        unsigned char bgr[3];
        bgr[0] = p[i * 3 + 2];
        bgr[1] = p[i * 3 + 1];
        bgr[2] = p[i * 3 + 0];
        fwrite(bgr, 1, 3, f);
    }
    fclose(f);
#ifndef _WIN32
    if (!headless) {
        /* Cross-platform GUI fallback: hand the PPM to the desktop image
           viewer.  Headless CI sets LAMBDA_GUI_HEADLESS=1 and never forks. */
        pid_t pid = fork();
        if (pid == 0) {
            execlp("xdg-open", "xdg-open", out, (char *)NULL);
            execlp("open", "open", out, (char *)NULL);
            _exit(127);
        }
    }
#endif
    fprintf(stderr, "lambda-gui: wrote %s (%lldx%lld)\n", out,
            (long long)w, (long long)h);
    fflush(stderr);
}
"""
