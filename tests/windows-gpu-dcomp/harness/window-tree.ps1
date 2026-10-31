param(
  [Parameter(Mandatory=$true)][int]$ProcessId,
  [Parameter(Mandatory=$true)][string]$OutJson
)
$ErrorActionPreference = "Stop"
Add-Type @"
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
public class GpuBWindowTree {
  public delegate bool EnumProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool EnumChildWindows(IntPtr parent, EnumProc cb, IntPtr lParam);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hWnd);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassName(IntPtr hWnd, StringBuilder s, int n);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr hWnd, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT r);
  [DllImport("user32.dll")] public static extern IntPtr GetParent(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern IntPtr GetAncestor(IntPtr hWnd, uint flags);
  [DllImport("user32.dll")] public static extern IntPtr GetWindow(IntPtr hWnd, uint cmd);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern IntPtr GetWindowLongPtr(IntPtr hWnd, int index);
  [DllImport("dwmapi.dll")] public static extern int DwmGetWindowAttribute(IntPtr hWnd, int attr, out int value, int size);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
  public class Node {
    public long hwnd; public long parent; public long owner; public long root;
    public int pid; public int tid; public string cls; public string title;
    public bool visible; public bool iconic; public int cloaked;
    public long style; public long ex_style;
    public int left, top, right, bottom;
    public bool top_level;
    public List<Node> children = new List<Node>();
  }
  static Node Make(IntPtr h, bool top) {
    var n = new Node();
    n.hwnd = h.ToInt64(); n.top_level = top;
    n.parent = GetParent(h).ToInt64();
    n.owner = GetWindow(h, 4).ToInt64(); // GW_OWNER
    n.root = GetAncestor(h, 2).ToInt64(); // GA_ROOT
    uint pid = 0; n.tid = (int)GetWindowThreadProcessId(h, out pid); n.pid = (int)pid;
    var cls = new StringBuilder(256); GetClassName(h, cls, 256); n.cls = cls.ToString();
    var title = new StringBuilder(512); GetWindowText(h, title, 512); n.title = title.ToString();
    n.visible = IsWindowVisible(h); n.iconic = IsIconic(h);
    int cloaked = 0; DwmGetWindowAttribute(h, 14, out cloaked, 4); n.cloaked = cloaked; // DWMWA_CLOAKED
    n.style = GetWindowLongPtr(h, -16).ToInt64();
    n.ex_style = GetWindowLongPtr(h, -20).ToInt64();
    RECT r; GetWindowRect(h, out r);
    n.left = r.Left; n.top = r.Top; n.right = r.Right; n.bottom = r.Bottom;
    return n;
  }
  static void WalkChildren(Node parent) {
    EnumChildWindows((IntPtr)parent.hwnd, delegate (IntPtr h, IntPtr l) {
      var n = Make(h, false);
      parent.children.Add(n);
      WalkChildren(n);
      return true;
    }, IntPtr.Zero);
  }
  public static List<Node> Collect(int targetPid) {
    var all = new List<Node>();
    var keep = new List<Node>();
    EnumWindows(delegate (IntPtr h, IntPtr l) {
      var n = Make(h, true);
      all.Add(n);
      return true;
    }, IntPtr.Zero);
    var childParents = new HashSet<long>();
    foreach (var n in all) {
      // Keep: windows of the target process, Chrome top-level windows of any
      // process, and hidden "Intermediate D3D Window" top-level popups.
      bool keepNode = n.pid == targetPid || n.cls == "Chrome_WidgetWin_1" ||
                      n.cls.IndexOf("Intermediate D3D", StringComparison.OrdinalIgnoreCase) >= 0;
      if (!keepNode) continue;
      WalkChildren(n);
      keep.Add(n);
      // Track parents so we can also find desktop-level child windows that are
      // not under a Chrome top-level (orphaned D3D child).
      childParents.Add(n.hwnd);
    }
    return keep;
  }
}
"@
$foreground = [GpuBWindowTree]::GetForegroundWindow().ToInt64()
$nodes = [GpuBWindowTree]::Collect($ProcessId)
$result = [pscustomobject]@{
  foreground_hwnd = $foreground
  target_pid = $ProcessId
  captured_at = (Get-Date).ToString("o")
  windows = $nodes
}
$result | ConvertTo-Json -Depth 12 | Set-Content -Path $OutJson -Encoding UTF8
$result | ConvertTo-Json -Depth 12
