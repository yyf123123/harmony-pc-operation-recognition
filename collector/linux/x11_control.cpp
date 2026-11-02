// Minimal real X11/XTest adapter for an isolated, authenticated test desktop.
// Build: c++ -std=c++17 x11_control.cpp -lX11 -ldl -o xdotool
// No window titles or credentials are emitted. The opt-in test clipboard
// reader emits controlled selection text only on an isolated test display.
#include <X11/Xlib.h>
#include <X11/Xatom.h>
#include <X11/keysym.h>
#include <X11/XKBlib.h>
#include <dlfcn.h>
#include <chrono>
#include <thread>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>
#include <cstdlib>

namespace {
Display* display;
using FakeKey = int (*)(Display*, unsigned int, Bool, unsigned long);
FakeKey fake_key;
unsigned long number(const std::string& text) {
    size_t end = 0;
    auto value = std::stoul(text, &end, 0);
    if (end != text.size() || !value) throw std::runtime_error("invalid window ID");
    return value;
}
unsigned long property(Window window, const char* name, Atom type) {
    Atom actual = 0; int format = 0; unsigned long size = 0, rest = 0;
    unsigned char* bytes = nullptr;
    int status = XGetWindowProperty(display, window, XInternAtom(display, name, False),
                                    0, 1, False, type, &actual, &format, &size, &rest, &bytes);
    unsigned long value = 0;
    if (status == Success && actual == type && format == 32 && size == 1)
        value = *reinterpret_cast<unsigned long*>(bytes);
    if (bytes) XFree(bytes);
    return value;
}
Window active() { return property(DefaultRootWindow(display), "_NET_ACTIVE_WINDOW", XA_WINDOW); }
void test_clipboard(const std::string& command,unsigned long pid) {
    const char* isolated=std::getenv("HARMONY_OPS_ISOLATED_TEST");
    if (!isolated || std::string(isolated)!="1" || property(active(),"_NET_WM_PID",XA_CARDINAL)!=pid)
        throw std::runtime_error("clipboard access requires the authorized isolated test window");
    const Atom selection=XInternAtom(display,"CLIPBOARD",False);
    if (command=="clear-clipboard") {
        XSetSelectionOwner(display,selection,None,CurrentTime); XSync(display,False); return;
    }
    Window receiver=XCreateSimpleWindow(display,DefaultRootWindow(display),0,0,1,1,0,0,0);
    const Atom target=XInternAtom(display,"UTF8_STRING",False), prop=XInternAtom(display,"HOP_TEST_CLIPBOARD",False);
    XConvertSelection(display,selection,target,prop,receiver,CurrentTime); XFlush(display);
    const auto until=std::chrono::steady_clock::now()+std::chrono::seconds(3);
    while (std::chrono::steady_clock::now()<until) {
        if (XPending(display)) {
            XEvent event; XNextEvent(display,&event);
            if (event.type==SelectionNotify && event.xselection.requestor==receiver) {
                Atom type; int format; unsigned long size,rest; unsigned char* bytes=nullptr;
                if (event.xselection.property==None) break;
                XGetWindowProperty(display,receiver,prop,0,16384,True,AnyPropertyType,&type,&format,&size,&rest,&bytes);
                bool valid=bytes && type==target && format==8 && rest==0 && size<=65536;
                if (valid) std::cout.write(reinterpret_cast<char*>(bytes),size);
                if (bytes) XFree(bytes);
                XDestroyWindow(display,receiver);
                if (!valid) throw std::runtime_error("controlled clipboard text unavailable");
                return;
            }
        } else std::this_thread::sleep_for(std::chrono::milliseconds(5));
    }
    XDestroyWindow(display,receiver);
    throw std::runtime_error("controlled clipboard did not respond");
}
void close_system_check(unsigned long pid) {
    // A known non-consent diagnostic blocks document focus on fresh launches.
    // Close only this exact WPS-owned window; never accept dialogs or change
    // font/install/update preferences. Titles are never emitted.
    Atom actual; int format; unsigned long size, rest; unsigned char* bytes = nullptr;
    XGetWindowProperty(display, DefaultRootWindow(display), XInternAtom(display, "_NET_CLIENT_LIST", False),
                       0, 4096, False, XA_WINDOW, &actual, &format, &size, &rest, &bytes);
    std::vector<Window> windows;
    if (bytes && actual == XA_WINDOW && format == 32) {
        auto* values = reinterpret_cast<unsigned long*>(bytes);
        windows.assign(values, values + size);
    }
    if (bytes) XFree(bytes);
    for (Window window : windows) {
        if (property(window, "_NET_WM_PID", XA_CARDINAL) != pid) continue;
        bytes = nullptr;
        const auto utf8 = XInternAtom(display, "UTF8_STRING", False);
        XGetWindowProperty(display, window, XInternAtom(display, "_NET_WM_NAME", False),
                           0, 256, False, utf8, &actual, &format, &size, &rest, &bytes);
        std::string title;
        if (bytes && actual == utf8 && format == 8 && rest == 0)
            title.assign(reinterpret_cast<char*>(bytes), size);
        if (bytes) XFree(bytes);
        if (title != "系统检查") continue;
        Atom* protocols = nullptr; int count = 0;
        const auto close = XInternAtom(display, "WM_DELETE_WINDOW", False);
        bool supported = false;
        if (XGetWMProtocols(display, window, &protocols, &count))
            for (int i = 0; i < count; ++i) supported = supported || protocols[i] == close;
        if (protocols) XFree(protocols);
        if (!supported) throw std::runtime_error("diagnostic has no normal close protocol");
        XEvent event{}; event.xclient.type = ClientMessage; event.xclient.window = window;
        event.xclient.message_type = XInternAtom(display, "WM_PROTOCOLS", False);
        event.xclient.format = 32; event.xclient.data.l[0] = close;
        event.xclient.data.l[1] = CurrentTime;
        XSendEvent(display, window, False, NoEventMask, &event); XSync(display, False);
    }
}
void key_event(KeySym symbol, bool pressed) {
    KeyCode code = XKeysymToKeycode(display, symbol);
    if (!code || !fake_key(display, code, pressed, CurrentTime))
        throw std::runtime_error("keyboard event unavailable");
}
void chord(const std::string& text) {
    std::vector<KeySym> symbols;
    size_t start = 0;
    while (start < text.size()) {
        auto end = text.find('+', start);
        auto part = text.substr(start, end - start);
        if (part == "ctrl") part = "Control_L";
        if (part == "shift") part = "Shift_L";
        if (part == "alt") part = "Alt_L";
        KeySym symbol = XStringToKeysym(part.c_str());
        if (symbol == NoSymbol) throw std::runtime_error("unsupported key");
        symbols.push_back(symbol);
        if (end == std::string::npos) break;
        start = end + 1;
    }
    if (symbols.empty()) throw std::runtime_error("empty key chord");
    for (auto symbol : symbols) key_event(symbol, true);
    for (auto it = symbols.rbegin(); it != symbols.rend(); ++it) key_event(*it, false);
    XSync(display, False);
}
void require_idle_modifiers() {
    char keys[32]; XQueryKeymap(display, keys);
    auto* modifiers = XGetModifierMapping(display);
    bool held = false;
    for (int i = 0; i < 8 * modifiers->max_keypermod; ++i) {
        auto code = modifiers->modifiermap[i];
        if (code && (keys[code / 8] & (1 << (code % 8)))) held = true;
    }
    XFreeModifiermap(modifiers);
    if (held) throw std::runtime_error("held modifier; refusing to alter keyboard state");
}
}
int main(int argc, char** argv) {
    try {
        if (argc < 2) throw std::runtime_error("command required");
        display = XOpenDisplay(nullptr);
        if (!display) throw std::runtime_error("authenticated X11 display unavailable");
        std::string command = argv[1];
        if ((command=="getclipboard" || command=="clear-clipboard") && argc==3) {
            test_clipboard(command,number(argv[2]));
        } else if (command == "close-system-check" && argc == 3) {
            close_system_check(number(argv[2]));
        } else if (command == "getactivewindow") {
            auto window = active();
            if (!window) throw std::runtime_error("no actual active window");
            std::cout << window << '\n';
        } else if (command == "getwindowpid" && argc == 3) {
            auto pid = property(number(argv[2]), "_NET_WM_PID", XA_CARDINAL);
            if (!pid) throw std::runtime_error("window PID unavailable");
            std::cout << pid << '\n';
        } else if (command == "windows") {
            Atom actual; int format; unsigned long size, rest; unsigned char* bytes = nullptr;
            XGetWindowProperty(display, DefaultRootWindow(display),
                XInternAtom(display, "_NET_CLIENT_LIST", False), 0, 4096, False,
                XA_WINDOW, &actual, &format, &size, &rest, &bytes);
            if (bytes && actual == XA_WINDOW && format == 32) {
                auto* windows = reinterpret_cast<unsigned long*>(bytes);
                for (unsigned long i = 0; i < size; ++i) {
                    auto pid = property(windows[i], "_NET_WM_PID", XA_CARDINAL);
                    std::cout << windows[i] << " " << pid << '\n';
                }
            }
            if (bytes) XFree(bytes);
        } else if (command == "windowactivate") {
            auto window = number(argv[argc - 1]);
            XEvent event{}; event.xclient.type = ClientMessage;
            event.xclient.window = window;
            event.xclient.message_type = XInternAtom(display, "_NET_ACTIVE_WINDOW", False);
            event.xclient.format = 32; event.xclient.data.l[0] = 1;
            event.xclient.data.l[1] = CurrentTime;
            XSendEvent(display, DefaultRootWindow(display), False,
                       SubstructureRedirectMask | SubstructureNotifyMask, &event);
            XFlush(display);
            for (int i = 0; i < 100 && active() != window; ++i)
                std::this_thread::sleep_for(std::chrono::milliseconds(20));
            if (active() != window) throw std::runtime_error("window activation not confirmed");
        } else if (command == "mousemove" && argc == 4) {
            int x = std::stoi(argv[2]), y = std::stoi(argv[3]);
            if (x < 0 || y < 0 || x >= DisplayWidth(display, DefaultScreen(display)) ||
                y >= DisplayHeight(display, DefaultScreen(display)))
                throw std::runtime_error("pointer outside test display");
            XWarpPointer(display, None, DefaultRootWindow(display), 0, 0, 0, 0, x, y);
            XSync(display, False);
        } else if (command == "click" && argc == 3) {
            auto button = std::stoi(argv[2]);
            if (button < 1 || button > 3) throw std::runtime_error("invalid button");
            void* library = dlopen("libXtst.so.6", RTLD_NOW | RTLD_LOCAL);
            using FakeButton = int (*)(Display*, unsigned int, Bool, unsigned long);
            auto button_event = library ? reinterpret_cast<FakeButton>(dlsym(library, "XTestFakeButtonEvent")) : nullptr;
            if (!button_event) throw std::runtime_error("XTest button entry unavailable");
            if (!button_event(display, button, True, CurrentTime) ||
                !button_event(display, button, False, CurrentTime))
                throw std::runtime_error("pointer event unavailable");
            // Xlib keeps extension callbacks until XCloseDisplay; retain the
            // runtime library for this short-lived process through that call.
            XSync(display, False);
        } else if (command == "key" || command == "type") {
            void* library = dlopen("libXtst.so.6", RTLD_NOW | RTLD_LOCAL);
            if (!library) throw std::runtime_error("XTest runtime unavailable");
            fake_key = reinterpret_cast<FakeKey>(dlsym(library, "XTestFakeKeyEvent"));
            if (!fake_key) throw std::runtime_error("XTest entry unavailable");
            require_idle_modifiers();
            int delay = 10, index = 2;
            for (; index < argc; ++index) {
                std::string option = argv[index];
                if (option == "--clearmodifiers") continue;
                if (option == "--delay" && index + 1 < argc) { delay = std::stoi(argv[++index]); continue; }
                if (option == "--") { ++index; break; }
                break;
            }
            if (index != argc - 1 || delay < 0 || delay > 1000)
                throw std::runtime_error("one bounded argument required");
            if (command == "key") chord(argv[index]);
            else {
                std::string payload = argv[index];
                if (payload.size() > 512) throw std::runtime_error("text too long");
                // Validate the whole payload before sending any key.
                for (unsigned char ch : payload)
                    if ((ch < 32 && ch!='\n') || ch > 126) throw std::runtime_error("ASCII test text required");
                for (unsigned char ch : payload) {
                    if (ch=='\n') { chord("Return"); std::this_thread::sleep_for(std::chrono::milliseconds(delay)); continue; }
                    KeySym symbol = ch;
                    auto code = XKeysymToKeycode(display, symbol);
                    if (!code) throw std::runtime_error("unmapped test character");
                    bool shift = XkbKeycodeToKeysym(display, code, 0, 0) != symbol;
                    if (shift && XkbKeycodeToKeysym(display, code, 0, 1) != symbol)
                        throw std::runtime_error("unsupported keyboard layout");
                    if (shift) key_event(XK_Shift_L, true);
                    key_event(symbol, true); key_event(symbol, false);
                    if (shift) key_event(XK_Shift_L, false);
                    XSync(display, False);
                    std::this_thread::sleep_for(std::chrono::milliseconds(delay));
                }
            }
        } else throw std::runtime_error("unsupported X11 command");
        XCloseDisplay(display);
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 2;
    }
}
