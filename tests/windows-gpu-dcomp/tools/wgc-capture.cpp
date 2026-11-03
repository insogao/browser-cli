// wgc-capture.exe - trusted per-window frame capture via Windows Graphics
// Capture (WGC) + D3D11. Diagnostics only (GPU-B DComp black-screen work).
//
// Usage:
//   wgc-capture.exe --hwnd <decimal|0xhex> --out <png-prefix> --json <path>
//                   [--frames N] [--interval-ms MS] [--timeout-ms MS]
//
// WGC window capture is DWM-backed: it captures the window's composed surface
// even when another window overlaps it, without needing foreground or a
// visible desktop region. This makes it a trusted "non-occluded" capture that
// does not steal foreground from other tests.
//
// Recorded evidence:
//   - raw HWND / PID / TID / class / title / window & client rects
//   - visibility, minimized/zoomed, foreground, DWM cloak + extended bounds
//   - window styles (including WS_EX_NOREDIRECTIONBITMAP / WS_EX_LAYERED)
//   - child HWNDs (Chrome_RenderWidgetHostHWND binding)
//   - WGC item size and per-frame SystemRelativeTime + QPC timestamps
//   - 9 client-area sample points + coarse color census + frame hash
//   - PNG of each captured frame (<prefix>.<index>.png)

#include <windows.h>
#include <d3d11.h>
#include <dwmapi.h>
#include <wincodec.h>
#include <wrl/client.h>

#include <winrt/base.h>
#include <winrt/Windows.Foundation.h>
#include <winrt/Windows.Graphics.h>
#include <winrt/Windows.Graphics.Capture.h>
#include <winrt/Windows.Graphics.DirectX.h>
#include <winrt/Windows.Graphics.DirectX.Direct3D11.h>
#include <windows.graphics.capture.interop.h>
#include <windows.graphics.directx.direct3d11.interop.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <map>
#include <mutex>
#include <string>
#include <vector>

#pragma comment(lib, "d3d11.lib")
#pragma comment(lib, "dxgi.lib")
#pragma comment(lib, "dwmapi.lib")
#pragma comment(lib, "windowscodecs.lib")
#pragma comment(lib, "ole32.lib")

namespace wgc = winrt::Windows::Graphics::Capture;
namespace wgdx = winrt::Windows::Graphics::DirectX;
namespace wgdxd3d = winrt::Windows::Graphics::DirectX::Direct3D11;
using Microsoft::WRL::ComPtr;

namespace {

struct Options {
  HWND hwnd = nullptr;
  std::wstring out_prefix;
  std::wstring json_path;
  int frames = 3;
  int interval_ms = 400;
  int timeout_ms = 15000;
};

[[noreturn]] void Fail(const char* what, HRESULT hr = E_FAIL) {
  std::fprintf(stderr, "wgc-capture: %s (hr=0x%08lx)\n", what,
               static_cast<unsigned long>(hr));
  std::exit(2);
}

std::string WideToUtf8(const std::wstring& w) {
  if (w.empty()) return {};
  int n = WideCharToMultiByte(CP_UTF8, 0, w.c_str(), -1, nullptr, 0, nullptr,
                              nullptr);
  std::string s(static_cast<size_t>(n) + 1, '\0');
  WideCharToMultiByte(CP_UTF8, 0, w.c_str(), -1, s.data(), n, nullptr, nullptr);
  s.resize(static_cast<size_t>(n) - 1);
  return s;
}

std::string JsonEscape(const std::string& in) {
  std::string out;
  for (unsigned char c : in) {
    switch (c) {
      case '"': out += "\\\""; break;
      case '\\': out += "\\\\"; break;
      case '\b': out += "\\b"; break;
      case '\f': out += "\\f"; break;
      case '\n': out += "\\n"; break;
      case '\r': out += "\\r"; break;
      case '\t': out += "\\t"; break;
      default:
        if (c < 0x20) {
          char buf[8];
          std::snprintf(buf, sizeof(buf), "\\u%04x", c);
          out += buf;
        } else {
          out += static_cast<char>(c);
        }
    }
  }
  return out;
}

std::string HexColor(uint32_t bgra) {
  char buf[16];
  std::snprintf(buf, sizeof(buf), "#%02X%02X%02X", (bgra >> 16) & 0xFF,
                (bgra >> 8) & 0xFF, bgra & 0xFF);
  return buf;
}

std::string Hex64(uintptr_t v) {
  char buf[32];
  std::snprintf(buf, sizeof(buf), "0x%llx",
                static_cast<unsigned long long>(v));
  return buf;
}

bool ParseArgs(int argc, wchar_t** argv, Options* out) {
  for (int i = 1; i < argc; ++i) {
    std::wstring a = argv[i];
    auto next = [&](const wchar_t* name) -> const wchar_t* {
      if (i + 1 >= argc) {
        std::fwprintf(stderr, L"wgc-capture: missing value for %ls\n", name);
        return nullptr;
      }
      return argv[++i];
    };
    if (a == L"--hwnd") {
      const wchar_t* v = next(L"--hwnd");
      if (!v) return false;
      out->hwnd = reinterpret_cast<HWND>(
          static_cast<uintptr_t>(_wcstoui64(v, nullptr, 0)));
    } else if (a == L"--out") {
      const wchar_t* v = next(L"--out");
      if (!v) return false;
      out->out_prefix = v;
    } else if (a == L"--json") {
      const wchar_t* v = next(L"--json");
      if (!v) return false;
      out->json_path = v;
    } else if (a == L"--frames") {
      const wchar_t* v = next(L"--frames");
      if (!v) return false;
      out->frames = _wtoi(v);
    } else if (a == L"--interval-ms") {
      const wchar_t* v = next(L"--interval-ms");
      if (!v) return false;
      out->interval_ms = _wtoi(v);
    } else if (a == L"--timeout-ms") {
      const wchar_t* v = next(L"--timeout-ms");
      if (!v) return false;
      out->timeout_ms = _wtoi(v);
    } else {
      std::fwprintf(stderr, L"wgc-capture: unknown argument %ls\n", a.c_str());
      return false;
    }
  }
  return out->hwnd && !out->out_prefix.empty() && !out->json_path.empty() &&
         out->frames > 0;
}

struct RectInfo {
  RECT window{};
  RECT client{};
  POINT client_origin{};
};

RectInfo QueryRects(HWND hwnd) {
  RectInfo r;
  if (!GetWindowRect(hwnd, &r.window)) Fail("GetWindowRect failed");
  if (!GetClientRect(hwnd, &r.client)) Fail("GetClientRect failed");
  if (!ClientToScreen(hwnd, &r.client_origin)) Fail("ClientToScreen failed");
  return r;
}

bool SavePng(const std::wstring& path, const BYTE* bgra, UINT width,
             UINT height, UINT stride, std::string* err) {
  ComPtr<IWICImagingFactory> factory;
  HRESULT hr = CoCreateInstance(CLSID_WICImagingFactory, nullptr,
                                CLSCTX_INPROC_SERVER, IID_PPV_ARGS(&factory));
  if (FAILED(hr)) {
    *err = "CoCreateInstance(WIC) failed";
    return false;
  }
  ComPtr<IWICStream> stream;
  hr = factory->CreateStream(&stream);
  if (SUCCEEDED(hr))
    hr = stream->InitializeFromFilename(path.c_str(), GENERIC_WRITE);
  ComPtr<IWICBitmapEncoder> encoder;
  if (SUCCEEDED(hr))
    hr = factory->CreateEncoder(GUID_ContainerFormatPng, nullptr, &encoder);
  if (SUCCEEDED(hr))
    hr = encoder->Initialize(stream.Get(), WICBitmapEncoderNoCache);
  ComPtr<IWICBitmapFrameEncode> frame;
  if (SUCCEEDED(hr)) hr = encoder->CreateNewFrame(&frame, nullptr);
  if (SUCCEEDED(hr)) hr = frame->Initialize(nullptr);
  if (SUCCEEDED(hr)) hr = frame->SetSize(width, height);
  WICPixelFormatGUID fmt = GUID_WICPixelFormat32bppBGRA;
  if (SUCCEEDED(hr)) hr = frame->SetPixelFormat(&fmt);
  if (SUCCEEDED(hr))
    hr = frame->WritePixels(height, stride, stride * height,
                            const_cast<BYTE*>(bgra));
  if (SUCCEEDED(hr)) hr = frame->Commit();
  if (SUCCEEDED(hr)) hr = encoder->Commit();
  if (FAILED(hr)) {
    char buf[64];
    std::snprintf(buf, sizeof(buf), "PNG write failed hr=0x%08lx",
                  static_cast<unsigned long>(hr));
    *err = buf;
    return false;
  }
  return true;
}

struct SamplePoint {
  const char* name;
  double fx;
  double fy;
};

const SamplePoint kSamplePoints[] = {
    {"center", 0.5, 0.5},       {"top_left", 0.08, 0.10},
    {"top_center", 0.5, 0.10},  {"top_right", 0.92, 0.10},
    {"left_mid", 0.08, 0.5},    {"right_mid", 0.92, 0.5},
    {"bottom_left", 0.08, 0.90}, {"bottom_center", 0.5, 0.90},
    {"bottom_right", 0.92, 0.90},
};

struct FrameSample {
  int index = -1;
  double system_relative_ms = -1;
  long long qpc_ms = -1;
  std::string png;
  int width = 0;
  int height = 0;
  std::vector<std::string> point_colors;
  std::vector<std::string> top_colors;
  int unique_colors = 0;
  bool all_dark = false;
  uint64_t hash = 0;
};

struct ChildWindow {
  std::string cls;
  std::string title;
  RECT rect{};
  bool visible = false;
  uintptr_t hwnd = 0;
  uintptr_t parent = 0;
  uintptr_t root = 0;
  DWORD pid = 0;
};

struct Capture {
  std::mutex mutex;
  Options opt;
  RectInfo rects;
  RECT frame_bounds{};
  UINT item_width = 0;
  UINT item_height = 0;
  wgc::Direct3D11CaptureFramePool pool{nullptr};
  wgc::GraphicsCaptureSession session{nullptr};
  ComPtr<ID3D11Device> device;
  ComPtr<ID3D11DeviceContext> context;
  std::vector<FrameSample> frames;
  std::string last_frame_error;
  std::atomic<int> saved{0};
  std::atomic<bool> done{false};
  std::atomic<long long> qpc_freq{1};
  long long next_allowed_qpc = 0;
};

std::wstring FramePath(const std::wstring& prefix, int index) {
  wchar_t buf[32];
  std::swprintf(buf, 32, L".%d.png", index);
  return prefix + buf;
}

uint64_t Fnv1a64(const BYTE* data, size_t size) {
  uint64_t h = 1469598103934665603ull;
  for (size_t i = 0; i < size; ++i) {
    h ^= data[i];
    h *= 1099511628211ull;
  }
  return h;
}

void HandleFrame(Capture* cap, const wgc::Direct3D11CaptureFrame& frame) {
  if (cap->saved.load() >= cap->opt.frames) return;
  std::lock_guard<std::mutex> lock(cap->mutex);
  if (static_cast<int>(cap->frames.size()) >= cap->opt.frames) return;

  LARGE_INTEGER now{};
  QueryPerformanceCounter(&now);
  const long long now_ms = now.QuadPart * 1000 / cap->qpc_freq.load();
  if (now_ms - cap->next_allowed_qpc < cap->opt.interval_ms) return;

  ComPtr<::Windows::Graphics::DirectX::Direct3D11::IDirect3DDxgiInterfaceAccess>
      access;
  HRESULT hr = reinterpret_cast<IUnknown*>(
                    winrt::get_abi(frame.Surface()))
                    ->QueryInterface(IID_PPV_ARGS(&access));
  winrt::com_ptr<ID3D11Texture2D> texture;
  if (SUCCEEDED(hr))
    hr = access->GetInterface(IID_PPV_ARGS(texture.put()));
  if (FAILED(hr)) {
    cap->last_frame_error = "IDirect3DDxgiInterfaceAccess::GetInterface failed";
    return;
  }
  D3D11_TEXTURE2D_DESC desc{};
  texture->GetDesc(&desc);
  D3D11_TEXTURE2D_DESC staging_desc = desc;
  staging_desc.Usage = D3D11_USAGE_STAGING;
  staging_desc.BindFlags = 0;
  staging_desc.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
  staging_desc.MiscFlags = 0;
  winrt::com_ptr<ID3D11Texture2D> staging;
  hr = cap->device->CreateTexture2D(&staging_desc, nullptr, staging.put());
  if (FAILED(hr)) {
    cap->last_frame_error = "CreateTexture2D(staging) failed";
    return;
  }
  cap->context->CopyResource(staging.get(), texture.get());
  D3D11_MAPPED_SUBRESOURCE mapped{};
  hr = cap->context->Map(staging.get(), 0, D3D11_MAP_READ, 0, &mapped);
  if (FAILED(hr)) {
    cap->last_frame_error = "Map(staging) failed";
    return;
  }

  FrameSample s;
  s.index = static_cast<int>(cap->frames.size());
  s.system_relative_ms =
      static_cast<double>(frame.SystemRelativeTime().count()) / 10000.0;
  s.qpc_ms = now_ms;
  s.width = static_cast<int>(desc.Width);
  s.height = static_cast<int>(desc.Height);
  s.png = WideToUtf8(FramePath(cap->opt.out_prefix, s.index));

  // Frame origin is the extended frame bounds (WGC excludes the drop shadow);
  // map the client area into frame pixels.
  const int fb_left = cap->frame_bounds.left;
  const int fb_top = cap->frame_bounds.top;
  const int fb_w = cap->frame_bounds.right - cap->frame_bounds.left;
  const int fb_h = cap->frame_bounds.bottom - cap->frame_bounds.top;
  const double scale_x = fb_w > 0 ? static_cast<double>(s.width) / fb_w : 1.0;
  const double scale_y = fb_h > 0 ? static_cast<double>(s.height) / fb_h : 1.0;
  const int cw = cap->rects.client.right - cap->rects.client.left;
  const int ch = cap->rects.client.bottom - cap->rects.client.top;
  const double client_x0 =
      (cap->rects.client_origin.x - fb_left) * scale_x;
  const double client_y0 =
      (cap->rects.client_origin.y - fb_top) * scale_y;
  const double client_w = cw * scale_x;
  const double client_h = ch * scale_y;

  const BYTE* base = static_cast<const BYTE*>(mapped.pData);
  auto pixel_at = [&](int x, int y) -> uint32_t {
    x = std::clamp(x, 0, s.width - 1);
    y = std::clamp(y, 0, s.height - 1);
    const BYTE* p = base + static_cast<size_t>(y) * mapped.RowPitch +
                    static_cast<size_t>(x) * 4;
    return static_cast<uint32_t>(p[0]) |
           (static_cast<uint32_t>(p[1]) << 8) |
           (static_cast<uint32_t>(p[2]) << 16) |
           (static_cast<uint32_t>(p[3]) << 24);
  };

  for (const auto& pt : kSamplePoints) {
    int x = static_cast<int>(client_x0 + client_w * pt.fx);
    int y = static_cast<int>(client_y0 + client_h * pt.fy);
    s.point_colors.push_back(HexColor(pixel_at(x, y)));
  }

  std::map<uint32_t, int> counts;
  int dark = 0;
  int total = 0;
  for (int y = 0; y < s.height; y += 4) {
    for (int x = 0; x < s.width; x += 4) {
      uint32_t c = pixel_at(x, y);
      int r = (c >> 16) & 0xFF, g = (c >> 8) & 0xFF, b = c & 0xFF;
      uint32_t q = ((r >> 3) << 10) | ((g >> 3) << 5) | (b >> 3);
      counts[q]++;
      if (r < 40 && g < 40 && b < 40) dark++;
      total++;
    }
  }
  s.unique_colors = static_cast<int>(counts.size());
  s.all_dark = total > 0 && dark == total;
  std::vector<std::pair<int, uint32_t>> top;
  for (const auto& [q, n] : counts) top.emplace_back(n, q);
  std::sort(top.begin(), top.end(),
            [](const auto& a, const auto& b) { return a.first > b.first; });
  for (size_t i = 0; i < top.size() && i < 8; ++i) {
    int r = ((top[i].second >> 10) & 0x1F) << 3;
    int g = ((top[i].second >> 5) & 0x1F) << 3;
    int b = (top[i].second & 0x1F) << 3;
    char buf[40];
    std::snprintf(buf, sizeof(buf), "#%02X%02X%02X:%d", r, g, b,
                  top[i].first);
    s.top_colors.push_back(buf);
  }
  s.hash = Fnv1a64(base, static_cast<size_t>(mapped.RowPitch) * s.height);

  std::string err;
  if (!SavePng(FramePath(cap->opt.out_prefix, s.index).c_str(), base,
               static_cast<UINT>(s.width), static_cast<UINT>(s.height),
               mapped.RowPitch, &err)) {
    cap->last_frame_error = err;
  }
  cap->context->Unmap(staging.get(), 0);
  cap->frames.push_back(std::move(s));
  cap->saved.fetch_add(1);
  cap->next_allowed_qpc = now_ms;
  if (cap->saved.load() >= cap->opt.frames) cap->done.store(true);
}

void CollectChildren(HWND parent, std::vector<ChildWindow>* out) {
  struct Ctx {
    std::vector<ChildWindow>* out;
  } ctx{out};
  EnumChildWindows(
      parent,
      [](HWND hwnd, LPARAM lp) -> BOOL {
        auto* c = reinterpret_cast<Ctx*>(lp);
        ChildWindow w;
        w.hwnd = reinterpret_cast<uintptr_t>(hwnd);
        wchar_t cls[256] = {};
        GetClassNameW(hwnd, cls, 256);
        w.cls = WideToUtf8(cls);
        wchar_t title[256] = {};
        GetWindowTextW(hwnd, title, 256);
        w.title = WideToUtf8(title);
        GetWindowRect(hwnd, &w.rect);
        w.visible = IsWindowVisible(hwnd) != FALSE;
        w.parent = reinterpret_cast<uintptr_t>(GetParent(hwnd));
        w.root = reinterpret_cast<uintptr_t>(GetAncestor(hwnd, GA_ROOT));
        DWORD cpid = 0;
        GetWindowThreadProcessId(hwnd, &cpid);
        w.pid = cpid;
        c->out->push_back(std::move(w));
        return TRUE;
      },
      reinterpret_cast<LPARAM>(&ctx));
}

std::string BuildJson(const Capture& cap, HRESULT capture_error, DWORD cloaked,
                      BOOL dwm_composition, UINT dpi,
                      const std::vector<ChildWindow>& children,
                      const std::string& session_error) {
  std::string j;
  j.reserve(8192);
  j += "{";
  j += "\"ok\":" + std::string(capture_error == S_OK ? "true" : "false") + ",";
  j += "\"session_error\":\"" + JsonEscape(session_error) + "\",";
  j += "\"hwnd\":" + std::to_string(reinterpret_cast<uintptr_t>(cap.opt.hwnd)) +
       ",";
  DWORD pid = 0;
  DWORD tid = GetWindowThreadProcessId(cap.opt.hwnd, &pid);
  j += "\"pid\":" + std::to_string(pid) + ",";
  j += "\"tid\":" + std::to_string(tid) + ",";
  {
    wchar_t cls[256] = {};
    GetClassNameW(cap.opt.hwnd, cls, 256);
    wchar_t title[512] = {};
    GetWindowTextW(cap.opt.hwnd, title, 512);
    j += "\"window_class\":\"" + JsonEscape(WideToUtf8(cls)) + "\",";
    j += "\"title\":\"" + JsonEscape(WideToUtf8(title)) + "\",";
  }
  j += "\"visible\":" + std::string(IsWindowVisible(cap.opt.hwnd) ? "true" : "false") + ",";
  j += "\"iconic\":" + std::string(IsIconic(cap.opt.hwnd) ? "true" : "false") + ",";
  j += "\"zoomed\":" + std::string(IsZoomed(cap.opt.hwnd) ? "true" : "false") + ",";
  j += "\"foreground\":" +
       std::string(GetForegroundWindow() == cap.opt.hwnd ? "true" : "false") +
       ",";
  j += "\"foreground_hwnd\":" +
       std::to_string(reinterpret_cast<uintptr_t>(GetForegroundWindow())) + ",";
  j += "\"dwm_cloaked\":" + std::to_string(cloaked) + ",";
  j += "\"dwm_composition_enabled\":" +
       std::string(dwm_composition ? "true" : "false") + ",";
  j += "\"dpi\":" + std::to_string(dpi) + ",";
  j += "\"style\":\"" +
       Hex64(static_cast<uintptr_t>(GetWindowLongPtrW(cap.opt.hwnd, GWL_STYLE))) +
       "\",";
  j += "\"ex_style\":\"" +
       Hex64(static_cast<uintptr_t>(GetWindowLongPtrW(cap.opt.hwnd, GWL_EXSTYLE))) +
       "\",";
  j += "\"has_noredirectionbitmap\":" +
       std::string((GetWindowLongPtrW(cap.opt.hwnd, GWL_EXSTYLE) &
                    WS_EX_NOREDIRECTIONBITMAP)
                       ? "true"
                       : "false") +
       ",";
  j += "\"has_layered\":" +
       std::string((GetWindowLongPtrW(cap.opt.hwnd, GWL_EXSTYLE) &
                    WS_EX_LAYERED)
                       ? "true"
                       : "false") +
       ",";
  RECT ext{};
  BOOL has_ext = SUCCEEDED(DwmGetWindowAttribute(
      cap.opt.hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, &ext, sizeof(ext)));
  j += "\"extended_frame_bounds\":{\"left\":" + std::to_string(ext.left) +
       ",\"top\":" + std::to_string(ext.top) +
       ",\"right\":" + std::to_string(ext.right) +
       ",\"bottom\":" + std::to_string(ext.bottom) + "},";
  j += "\"extended_frame_bounds_ok\":" +
       std::string(has_ext ? "true" : "false") + ",";
  j += "\"window_rect\":{\"left\":" + std::to_string(cap.rects.window.left) +
       ",\"top\":" + std::to_string(cap.rects.window.top) +
       ",\"right\":" + std::to_string(cap.rects.window.right) +
       ",\"bottom\":" + std::to_string(cap.rects.window.bottom) +
       ",\"width\":" +
       std::to_string(cap.rects.window.right - cap.rects.window.left) +
       ",\"height\":" +
       std::to_string(cap.rects.window.bottom - cap.rects.window.top) + "},";
  j += "\"client_rect\":{\"width\":" +
       std::to_string(cap.rects.client.right - cap.rects.client.left) +
       ",\"height\":" +
       std::to_string(cap.rects.client.bottom - cap.rects.client.top) + "},";
  j += "\"client_origin\":{\"x\":" + std::to_string(cap.rects.client_origin.x) +
       ",\"y\":" + std::to_string(cap.rects.client_origin.y) + "},";
  j += "\"capture_item_size\":{\"width\":" + std::to_string(cap.item_width) +
       ",\"height\":" + std::to_string(cap.item_height) + "},";
  j += "\"children\":[";
  for (size_t i = 0; i < children.size(); ++i) {
    if (i) j += ",";
    const auto& c = children[i];
    j += "{\"hwnd\":" + std::to_string(c.hwnd) + ",\"parent\":" +
         std::to_string(c.parent) + ",\"root\":" + std::to_string(c.root) +
         ",\"pid\":" + std::to_string(c.pid) + ",\"class\":\"" +
         JsonEscape(c.cls) + "\",\"title\":\"" + JsonEscape(c.title) +
         "\",\"visible\":" + (c.visible ? "true" : "false") +
         ",\"rect\":{\"left\":" + std::to_string(c.rect.left) +
         ",\"top\":" + std::to_string(c.rect.top) +
         ",\"right\":" + std::to_string(c.rect.right) +
         ",\"bottom\":" + std::to_string(c.rect.bottom) + "}}";
  }
  j += "],";
  j += "\"frames\":[";
  {
    std::lock_guard<std::mutex> lock(
        const_cast<std::mutex&>(cap.mutex));
    for (size_t i = 0; i < cap.frames.size(); ++i) {
      if (i) j += ",";
      const auto& f = cap.frames[i];
      j += "{\"index\":" + std::to_string(f.index) +
           ",\"png\":\"" + JsonEscape(f.png) +
           "\",\"width\":" + std::to_string(f.width) +
           ",\"height\":" + std::to_string(f.height) +
           ",\"system_relative_ms\":" + std::to_string(f.system_relative_ms) +
           ",\"qpc_ms\":" + std::to_string(f.qpc_ms) +
           ",\"unique_colors\":" + std::to_string(f.unique_colors) +
           ",\"all_dark\":" + (f.all_dark ? "true" : "false") +
           ",\"hash\":" + std::to_string(f.hash) + ",\"points\":[";
      for (size_t k = 0; k < f.point_colors.size(); ++k) {
        if (k) j += ",";
        j += "\"" + f.point_colors[k] + "\"";
      }
      j += "],\"top_colors\":[";
      for (size_t k = 0; k < f.top_colors.size(); ++k) {
        if (k) j += ",";
        j += "\"" + f.top_colors[k] + "\"";
      }
      j += "]}";
    }
  }
  j += "],";
  j += "\"last_frame_error\":\"" + JsonEscape(cap.last_frame_error) + "\"";
  j += "}";
  return j;
}

}  // namespace

int wmain(int argc, wchar_t** argv) {
  Options opt;
  if (!ParseArgs(argc, argv, &opt)) {
    std::fprintf(stderr,
                 "usage: wgc-capture --hwnd <hwnd> --out <png-prefix> --json "
                 "<path> [--frames N] [--interval-ms MS] [--timeout-ms MS]\n");
    return 1;
  }
  winrt::init_apartment(winrt::apartment_type::multi_threaded);
  if (!wgc::GraphicsCaptureSession::IsSupported()) {
    Fail("Windows Graphics Capture is not supported on this system");
  }

  Capture cap;
  cap.opt = opt;
  if (!IsWindow(opt.hwnd)) Fail("target HWND is not a window");
  cap.rects = QueryRects(opt.hwnd);
  cap.frame_bounds = cap.rects.window;
  DWORD cloaked = 0;
  DwmGetWindowAttribute(opt.hwnd, DWMWA_CLOAKED, &cloaked, sizeof(cloaked));
  RECT ext{};
  if (SUCCEEDED(DwmGetWindowAttribute(opt.hwnd, DWMWA_EXTENDED_FRAME_BOUNDS,
                                      &ext, sizeof(ext)))) {
    cap.frame_bounds = ext;
  }
  BOOL dwm_composition = FALSE;
  DwmIsCompositionEnabled(&dwm_composition);
  UINT dpi = GetDpiForWindow(opt.hwnd);
  std::vector<ChildWindow> children;
  CollectChildren(opt.hwnd, &children);
  UINT flags = D3D11_CREATE_DEVICE_BGRA_SUPPORT;
  D3D_FEATURE_LEVEL level = D3D_FEATURE_LEVEL_11_0;
  HRESULT hr = D3D11CreateDevice(
      nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr, flags, &level, 1,
      D3D11_SDK_VERSION, cap.device.GetAddressOf(), nullptr,
      cap.context.GetAddressOf());
  if (FAILED(hr)) {
    hr = D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_WARP, nullptr, flags,
                           &level, 1, D3D11_SDK_VERSION,
                           cap.device.GetAddressOf(), nullptr,
                           cap.context.GetAddressOf());
  }
  if (FAILED(hr)) Fail("D3D11CreateDevice failed", hr);

  winrt::com_ptr<IDXGIDevice> dxgi_device;
  hr = cap.device->QueryInterface(IID_PPV_ARGS(dxgi_device.put()));
  if (FAILED(hr)) Fail("ID3D11Device -> IDXGIDevice failed", hr);
  winrt::Windows::Foundation::IInspectable inspectable{nullptr};
  hr = CreateDirect3D11DeviceFromDXGIDevice(
      dxgi_device.get(),
      reinterpret_cast<IInspectable**>(winrt::put_abi(inspectable)));
  if (FAILED(hr)) Fail("CreateDirect3D11DeviceFromDXGIDevice failed", hr);
  wgdxd3d::IDirect3DDevice d3d_device =
      inspectable.as<wgdxd3d::IDirect3DDevice>();
  auto factory = winrt::get_activation_factory<wgc::GraphicsCaptureItem>();
  ComPtr<::IGraphicsCaptureItemInterop> interop;
  hr = reinterpret_cast<IUnknown*>(winrt::get_abi(factory))
           ->QueryInterface(IID_PPV_ARGS(&interop));
  if (FAILED(hr)) Fail("IGraphicsCaptureItemInterop QI failed", hr);
  wgc::GraphicsCaptureItem item{nullptr};
  hr = interop->CreateForWindow(
      opt.hwnd, winrt::guid_of<wgc::GraphicsCaptureItem>(),
      reinterpret_cast<void**>(winrt::put_abi(item)));
  if (FAILED(hr)) {
    Fail("IGraphicsCaptureItemInterop::CreateForWindow failed", hr);
  }
  const auto item_size = item.Size();
  cap.item_width = static_cast<UINT>(item_size.Width);
  cap.item_height = static_cast<UINT>(item_size.Height);
  cap.pool = wgc::Direct3D11CaptureFramePool::CreateFreeThreaded(
      d3d_device, wgdx::DirectXPixelFormat::B8G8R8A8UIntNormalized, 3,
      item_size);
  cap.session = cap.pool.CreateCaptureSession(item);
  try {
    cap.session.IsCursorCaptureEnabled(false);
  } catch (...) {
  }
  try {
    cap.session.IsBorderRequired(false);
  } catch (...) {
  }

  LARGE_INTEGER freq{};
  QueryPerformanceFrequency(&freq);
  cap.qpc_freq.store(freq.QuadPart);

  auto handler = [&cap](const wgc::Direct3D11CaptureFramePool& sender,
                        const winrt::Windows::Foundation::IInspectable&) {
    auto frame = sender.TryGetNextFrame();
    if (frame) HandleFrame(&cap, frame);
  };
  auto token = cap.pool.FrameArrived(handler);

  HRESULT session_hr = S_OK;
  std::string session_error;
  try {
    cap.session.StartCapture();
  } catch (const winrt::hresult_error& e) {
    session_hr = e.code();
    session_error = WideToUtf8(std::wstring(e.message().c_str()));
  }

  if (SUCCEEDED(session_hr)) {
    const auto deadline =
        std::chrono::steady_clock::now() + std::chrono::milliseconds(opt.timeout_ms);
    while (!cap.done.load() && std::chrono::steady_clock::now() < deadline) {
      Sleep(20);
    }
  }

  cap.pool.FrameArrived(token);
  {
    // Wait for any FrameArrived handler that was already dispatched before
    // revoking the token, so it cannot race session/pool teardown.
    std::lock_guard<std::mutex> lock(cap.mutex);
  }
  try {
    cap.session.Close();
  } catch (...) {
  }
  try {
    cap.pool.Close();
  } catch (...) {
  }
  std::string json = BuildJson(cap, session_hr, cloaked, dwm_composition, dpi,
                               children, session_error);
  FILE* f = _wfopen(opt.json_path.c_str(), L"wb");
  if (!f) Fail("cannot open JSON output");
  std::fwrite(json.data(), 1, json.size(), f);
  std::fclose(f);

  std::printf("%s\n", json.c_str());
  return (SUCCEEDED(session_hr) && !cap.frames.empty()) ? 0 : 3;
}
