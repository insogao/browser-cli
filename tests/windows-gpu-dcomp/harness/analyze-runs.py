#!/usr/bin/env python3
"""Consolidate GPU-B DComp A/B evidence from gpu-dcomp-ab.mjs run roots.

Usage:
  python analyze-runs.py <root> [<root> ...]
  python analyze-runs.py --out D:/c/logs/gpu-b2/evidence-all.json <root> ...

Reads summary.json and trace-startup.json produced by gpu-dcomp-ab.mjs and
writes evidence.json into each run root plus a combined JSON.
"""

import json
import os
import re
import sys


def load(path):
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


def verdict(cap):
    if not cap:
        return None
    data = cap.get("data") or {}
    frames = data.get("frames") or []
    if not frames:
        return {
            "frames": 0,
            "error": data.get("last_frame_error") or cap.get("stderr", "")[:400],
        }
    return {
        "frames": len(frames),
        "unique_colors": [f.get("unique_colors") for f in frames],
        "all_dark": [f.get("all_dark") for f in frames],
        "hashes": [f.get("hash") for f in frames],
        "points": [f.get("points") for f in frames],
        "top_colors": [f.get("top_colors") for f in frames],
        "times_ms": [f.get("system_relative_ms") for f in frames],
        "sizes": [[f.get("width"), f.get("height")] for f in frames],
    }


def key_events(trace_path):
    try:
        raw = load(trace_path)
    except Exception as e:
        return {"error": str(e)[:300]}
    evs = raw if isinstance(raw, list) else raw.get("traceEvents", [])
    wanted = {
        "Surface::CommitFrame",
        "Surface::ActivateFrame",
        "Surface::RequestCopyOfOutput",
        "Surface::RequestCopyOfOutputOnRootRenderPass",
        "Surface::TakeCopyOutputRequestsFromClient",
        "Display::DrawAndSwap",
        "SkiaOutputSurfaceImplOnGpu::SwapBuffers",
        "DCompPresenter::Present",
        "DCompPresenter::CheckPendingFrames",
        "DCLayerTree::CommitAndClearPendingOverlays",
        "SkiaOutputDevice->ScheduleOverlays",
        "SkiaOutputDeviceDComp::BeginOverlayAccess",
        "SkiaOutputDeviceDawnD3D11BltMode::Present1",
        "SkiaOutputDeviceDawnSwapChain::PresentImpl",
        "NativeViewGLSurfaceEGL:PostSubBuffer",
        "FramePresented",
    }
    counts = {}
    first = {}
    last = {}
    surfaces = set()
    for e in evs:
        name = e.get("name")
        if name in wanted:
            counts[name] = counts.get(name, 0) + 1
            if name not in first:
                first[name] = {"ts": e.get("ts"), "dur": e.get("dur"), "args": e.get("args")}
            last[name] = {"ts": e.get("ts"), "dur": e.get("dur"), "args": e.get("args")}
        args = e.get("args") or {}
        for v in args.values():
            if isinstance(v, str) and v.startswith("SurfaceId("):
                surfaces.add(v)
    # Ordered first-swap sequence, useful to identify the first divergence.
    seq = []
    for e in evs:
        name = e.get("name")
        if name in wanted and name not in (
            "SkiaOutputDeviceDawnD3D11BltMode::Present1",
            "SkiaOutputDeviceDawnSwapChain::PresentImpl",
            "NativeViewGLSurfaceEGL:PostSubBuffer",
            "FramePresented",
            "DCompPresenter::CheckPendingFrames",
        ):
            seq.append({"name": name, "ts": e.get("ts"), "dur": e.get("dur")})
        if len(seq) >= 14:
            break
    return {
        "total_events": len(evs),
        "counts": counts,
        "first": first,
        "last": last,
        "surface_ids": sorted(surfaces)[:40],
        "first_sequence": seq,
    }


def gpu_info(summary):
    si = summary.get("sys_info") or {}
    gpu = si.get("gpu") or {}
    aux = gpu.get("auxAttributes") or {}
    features = gpu.get("featureStatus") or {}
    devices = gpu.get("devices") or []
    return {
        "displayType": aux.get("displayType"),
        "skiaBackendType": aux.get("skiaBackendType"),
        "directComposition": aux.get("directComposition"),
        "inProcessGpu": aux.get("inProcessGpu"),
        "glRenderer": aux.get("glRenderer"),
        "devices": [
            {k: d.get(k) for k in ("deviceString", "driverVendor", "driverVersion")}
            for d in devices
        ],
        "feature_status": features,
    }


def top_children(summary):
    out = []
    for key in ("wgc_visible", "wgc_bottom"):
        w = summary.get(key) or {}
        data = w.get("data") or {}
        if not data:
            continue
        out.append(
            {
                "tag": key,
                "hwnd": data.get("hwnd"),
                "pid": data.get("pid"),
                "visible": data.get("visible"),
                "foreground": data.get("foreground"),
                "foreground_hwnd": data.get("foreground_hwnd"),
                "dwm_cloaked": data.get("dwm_cloaked"),
                "style": data.get("style"),
                "ex_style": data.get("ex_style"),
                "has_noredirectionbitmap": data.get("has_noredirectionbitmap"),
                "has_layered": data.get("has_layered"),
                "window_rect": data.get("window_rect"),
                "client_rect": data.get("client_rect"),
                "client_origin": data.get("client_origin"),
                "capture_item_size": data.get("capture_item_size"),
                "children": [
                    {
                        "hwnd": c.get("hwnd"),
                        "pid": c.get("pid"),
                        "parent": c.get("parent"),
                        "root": c.get("root"),
                        "class": c.get("class"),
                        "visible": c.get("visible"),
                        "rect": c.get("rect"),
                    }
                    for c in (data.get("children") or [])
                ],
            }
        )
    return out


def one(root):
    summary = load(os.path.join(root, "summary.json"))
    trace = key_events(os.path.join(root, "trace-startup.json"))
    cdp_vis = (summary.get("cdp_visible") or {}).get("sample") or {}
    cdp_bot = (summary.get("cdp_bottom") or {}).get("sample") or {}
    cdp_hid = (summary.get("cdp_hidden") or {}).get("sample") or {}
    ev = {
        "root": root,
        "variant": summary.get("variant"),
        "chrome": summary.get("chromeExe"),
        "flags": summary.get("flags"),
        "ok": summary.get("ok"),
        "browser_pid": summary.get("browserPid"),
        "hwnd": summary.get("hwnd"),
        "show_state": summary.get("show_state"),
        "early_show": summary.get("early_show"),
        "page_state": summary.get("pageState"),
        "page_final": summary.get("pageFinal"),
        "window_before_show": [
            {
                "hwnd": w.get("hwnd"),
                "cls": w.get("cls"),
                "visible": w.get("visible"),
                "rect": [w.get("left"), w.get("top"), w.get("right"), w.get("bottom")],
                "children": [
                    {"cls": c.get("cls"), "hwnd": c.get("hwnd"), "visible": c.get("visible")}
                    for c in (w.get("children") or [])
                ],
            }
            for w in ((summary.get("window_tree_pre_show") or {}).get("windows") or [])
            if w.get("pid") == summary.get("browserPid") and w.get("cls") == "Chrome_WidgetWin_1"
        ],
        "window_after_show": [
            {
                "hwnd": w.get("hwnd"),
                "cls": w.get("cls"),
                "visible": w.get("visible"),
                "rect": [w.get("left"), w.get("top"), w.get("right"), w.get("bottom")],
                "children": [
                    {"cls": c.get("cls"), "hwnd": c.get("hwnd"), "visible": c.get("visible")}
                    for c in (w.get("children") or [])
                ],
            }
            for w in ((summary.get("window_tree_post_show") or {}).get("windows") or [])
            if w.get("pid") == summary.get("browserPid") and w.get("cls") == "Chrome_WidgetWin_1"
        ],
        "gpu": gpu_info(summary),
        "cdp_visible": {
            "colorful": cdp_vis.get("colorful"),
            "unique": cdp_vis.get("sample_unique_colors"),
            "saturated": cdp_vis.get("sample_saturated"),
            "all_dark": cdp_vis.get("all_dark"),
        },
        "cdp_bottom": {
            "colorful": cdp_bot.get("colorful"),
            "unique": cdp_bot.get("sample_unique_colors"),
            "saturated": cdp_bot.get("sample_saturated"),
            "all_dark": cdp_bot.get("all_dark"),
        },
        "cdp_hidden": {
            "colorful": cdp_hid.get("colorful"),
            "unique": cdp_hid.get("sample_unique_colors"),
            "saturated": cdp_hid.get("sample_saturated"),
            "all_dark": cdp_hid.get("all_dark"),
        },
        "hide_state": summary.get("hide_state"),
        "wgc_visible": verdict(summary.get("wgc_visible")),
        "wgc_bottom": verdict(summary.get("wgc_bottom")),
        "wgc_children": top_children(summary),
        "trace": trace,
    }
    with open(os.path.join(root, "evidence.json"), "w", encoding="utf-8") as f:
        json.dump(ev, f, indent=1)
    return ev


def color_summary(v):
    if not v or not v.get("frames"):
        return "NO-FRAME"
    pts = v["points"][0]
    uniq = len(set(pts))
    def saturated(h):
        r, g, b = int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)
        return max(r, g, b) - min(r, g, b) >= 80
    sat = sum(1 for h in pts if saturated(h))
    return f"{uniq} distinct points / {sat} saturated / {'DARK' if v['all_dark'][0] else 'not-all-dark'} / hash={v['hashes'][0]}"


def main():
    argv = sys.argv[1:]
    out = None
    if "--out" in argv:
        i = argv.index("--out")
        out = argv[i + 1]
        del argv[i : i + 2]
    roots = argv
    if not roots:
        print(__doc__)
        return 2
    all_ev = [one(r) for r in roots]
    if out:
        with open(out, "w", encoding="utf-8") as f:
            json.dump(all_ev, f, indent=1)
    for ev in all_ev:
        t = ev["trace"]
        counts = t.get("counts", {}) if isinstance(t, dict) else {}
        print("=" * 100)
        print(ev["variant"], ev["root"])
        print("  chrome:", ev["chrome"], "flags:", " ".join(ev["flags"] or []))
        print("  hwnd:", ev["hwnd"], "pid:", ev["browser_pid"], "pre_show:", ev["window_before_show"])
        print("  post_show:", ev["window_after_show"])
        print("  gpu:", {k: ev["gpu"].get(k) for k in ("displayType", "directComposition", "inProcessGpu")})
        print("  cdp visible:", ev["cdp_visible"], " bottom:", ev["cdp_bottom"], " hidden:", ev["cdp_hidden"], " hide:", ev["hide_state"])
        print("  wgc visible:", color_summary(ev["wgc_visible"]))
        print("  wgc bottom :", color_summary(ev["wgc_bottom"]))
        for k in (
            "DCompPresenter::Present",
            "DCLayerTree::CommitAndClearPendingOverlays",
            "SkiaOutputDeviceDawnD3D11BltMode::Present1",
            "SkiaOutputDeviceDawnSwapChain::PresentImpl",
            "NativeViewGLSurfaceEGL:PostSubBuffer",
            "Display::DrawAndSwap",
            "SkiaOutputSurfaceImplOnGpu::SwapBuffers",
            "FramePresented",
        ):
            print(f"  event {k}: {counts.get(k, 0)}")
        print("  surfaces:", (t.get("surface_ids") or [])[:4])
        print("  first sequence:", [(s["name"], s["ts"]) for s in (t.get("first_sequence") or [])])
        if ev["wgc_children"]:
            c = ev["wgc_children"][0]
            print("  children:", [(x["class"], x["hwnd"], x["pid"], x["parent"], x["visible"]) for x in c["children"]])
    return 0


if __name__ == "__main__":
    sys.exit(main())
