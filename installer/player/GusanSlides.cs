// GusanSlides.exe: plays the installer's intro video (installer\wizard\gusan_intro.mp4) in a window that sits
// on the Installing page of Setup, as its own program.
//
// Why a program of its own: Setup unpacks files on the same thread that runs its timers, so anything Setup
// draws itself (the slideshow of pictures) can only move between two files: a still picture, about once a
// second. This program has its own message loop, so the video keeps playing while Setup works.
//
// How it sits on the page: a window without frame, taskbar button or focus, OWNED by Setup's wizard window
// (so it is always above it, is hidden when Setup is minimised and never covers other programs), moved onto
// the Installing page every few milliseconds, shown only while that page is on screen and hidden (and
// paused) otherwise. The mouse goes through it.
//
// Made to fail quietly: if the video can't be opened (no Windows Media Player components, no H.264 decoder:
// "N" editions of Windows), or Setup does not show the page in time, it ends and Setup falls back to the
// still slideshow. It ends by itself when Setup's window is gone.
//
// Setup finds it by its window title: "GusanSlidesStarting" while it loads, "GusanSlidesVideo" once the
// video is open and it plays. Closing the window (WM_CLOSE) ends it.
//
//   GusanSlides.exe --owner <hwnd> --page <hwnd> --x <px> --y <px> --w <px> --h <px> --video <file> [--deadline-ms <ms>]
//
// x and y are the video's place on the page, in pixels from the page's top left corner (the page is the
// Installing page of the wizard). Set GUSAN_SLIDES_LOG=<file> to get a line per event (tests).
//
// Built with the C# 5 compiler that ships with Windows (installer\player\build.ps1): no extra tools.

using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Interop;
using System.Windows.Media;
using System.Windows.Threading;

namespace GusanSlides
{
    internal static class Native
    {
        [StructLayout(LayoutKind.Sequential)]
        public struct POINT { public int X, Y; }

        public const int GWL_EXSTYLE = -20;
        public const int WS_EX_TOOLWINDOW = 0x00000080;
        public const int WS_EX_NOACTIVATE = 0x08000000;
        public const uint SWP_NOSIZE = 0x0001, SWP_NOMOVE = 0x0002, SWP_NOZORDER = 0x0004, SWP_NOACTIVATE = 0x0010,
                          SWP_SHOWWINDOW = 0x0040;
        public const int SW_HIDE = 0, SW_SHOWNA = 8;
        public const int WM_MOUSEACTIVATE = 0x0021, WM_NCHITTEST = 0x0084;
        public const int MA_NOACTIVATE = 3, HTTRANSPARENT = -1;

        [DllImport("user32.dll")] public static extern bool IsWindow(IntPtr h);
        [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
        [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
        [DllImport("user32.dll")] public static extern bool ClientToScreen(IntPtr h, ref POINT p);
        [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int cmd);
        [DllImport("user32.dll")] public static extern int GetWindowLong(IntPtr h, int index);
        [DllImport("user32.dll")] public static extern int SetWindowLong(IntPtr h, int index, int value);
        [DllImport("user32.dll")]
        public static extern bool SetWindowPos(IntPtr h, IntPtr after, int x, int y, int cx, int cy, uint flags);
    }

    internal sealed class Options
    {
        public IntPtr Owner, Page;
        public int X, Y, W, H, DeadlineMs = 8000;
        public string Video;

        public static Options Parse(string[] a)
        {
            Options o = new Options();
            for (int i = 0; i + 1 < a.Length; i += 2)
            {
                string k = a[i], v = a[i + 1];
                long n;
                if (k == "--video") o.Video = v;
                else if (long.TryParse(v, out n))
                {
                    if (k == "--owner") o.Owner = new IntPtr(n);
                    else if (k == "--page") o.Page = new IntPtr(n);
                    else if (k == "--x") o.X = (int)n;
                    else if (k == "--y") o.Y = (int)n;
                    else if (k == "--w") o.W = (int)n;
                    else if (k == "--h") o.H = (int)n;
                    else if (k == "--deadline-ms") o.DeadlineMs = (int)n;
                }
            }
            if (o.Owner == IntPtr.Zero || o.Page == IntPtr.Zero || o.W < 16 || o.H < 16 || string.IsNullOrEmpty(o.Video))
                return null;
            return o;
        }
    }

    internal sealed class Host : Window
    {
        private const string TitleStarting = "GusanSlidesStarting", TitleReady = "GusanSlidesVideo";

        private readonly Options opt;
        private readonly MediaElement media;
        private readonly DispatcherTimer tick;
        private readonly Stopwatch clock = Stopwatch.StartNew();
        private readonly string logPath = Environment.GetEnvironmentVariable("GUSAN_SLIDES_LOG");
        private IntPtr hwnd;
        private bool ready, shown;
        private Native.POINT last;
        private long lastPosLog, lastRender, maxRenderGap, renderFrames;

        public Host(Options o)
        {
            opt = o;
            WindowStyle = WindowStyle.None;
            ResizeMode = ResizeMode.NoResize;
            ShowInTaskbar = false;
            ShowActivated = false;
            Topmost = false;
            Focusable = false;
            Background = Brushes.White;
            Title = TitleStarting;
            WindowStartupLocation = WindowStartupLocation.Manual;
            Left = -32000;
            Top = -32000;
            Width = 64;
            Height = 32;
            new WindowInteropHelper(this).Owner = o.Owner;

            media = new MediaElement();
            media.LoadedBehavior = MediaState.Manual;
            media.UnloadedBehavior = MediaState.Manual;
            media.Stretch = Stretch.Fill;
            media.IsMuted = true;
            media.Volume = 0;
            media.ScrubbingEnabled = true;
            RenderOptions.SetBitmapScalingMode(media, BitmapScalingMode.HighQuality);
            media.MediaOpened += OnOpened;
            media.MediaFailed += delegate(object s, ExceptionRoutedEventArgs e)
            {
                Log("failed: " + (e.ErrorException != null ? e.ErrorException.Message : "?"));
                Quit(4);
            };
            media.MediaEnded += delegate { media.Position = TimeSpan.Zero; media.Play(); };
            Content = media;

            SourceInitialized += OnSourceInitialized;
            Loaded += delegate
            {
                media.Source = new Uri(opt.Video);
                media.Play();                       // Manual mode opens the file only when asked to play
                Log("loaded");
            };
            Closed += delegate { Log("closed"); };

            tick = new DispatcherTimer(DispatcherPriority.Normal);
            tick.Interval = TimeSpan.FromMilliseconds(16);
            tick.Tick += OnTick;
            tick.Start();

            if (logPath != null)
            {
                // Tests only: how steadily the window is redrawn (frames and the longest gap, per second).
                CompositionTarget.Rendering += delegate
                {
                    long now = clock.ElapsedMilliseconds;
                    if (lastRender != 0 && now - lastRender > maxRenderGap) maxRenderGap = now - lastRender;
                    lastRender = now;
                    renderFrames++;
                };
            }
        }

        private void OnSourceInitialized(object sender, EventArgs e)
        {
            hwnd = new WindowInteropHelper(this).Handle;
            int ex = Native.GetWindowLong(hwnd, Native.GWL_EXSTYLE);
            Native.SetWindowLong(hwnd, Native.GWL_EXSTYLE, ex | Native.WS_EX_TOOLWINDOW | Native.WS_EX_NOACTIVATE);
            HwndSource src = HwndSource.FromHwnd(hwnd);
            if (src != null) src.AddHook(Hook);
        }

        // The mouse goes through the video to what is behind it, and the window never takes the focus.
        private IntPtr Hook(IntPtr h, int msg, IntPtr w, IntPtr l, ref bool handled)
        {
            if (msg == Native.WM_NCHITTEST) { handled = true; return new IntPtr(Native.HTTRANSPARENT); }
            if (msg == Native.WM_MOUSEACTIVATE) { handled = true; return new IntPtr(Native.MA_NOACTIVATE); }
            return IntPtr.Zero;
        }

        private void OnOpened(object sender, RoutedEventArgs e)
        {
            media.Pause();                          // it plays when the page is on screen
            media.Position = TimeSpan.Zero;
            ready = true;
            Title = TitleReady;
            Log("opened " + media.NaturalVideoWidth + "x" + media.NaturalVideoHeight + " " +
                (media.NaturalDuration.HasTimeSpan ? media.NaturalDuration.TimeSpan.TotalSeconds.ToString("0.0") : "?") + " s");
        }

        private void OnTick(object sender, EventArgs e)
        {
            if (hwnd == IntPtr.Zero) return;
            if (!Native.IsWindow(opt.Owner)) { Log("owner gone"); Quit(0); return; }
            if (!ready)
            {
                if (clock.ElapsedMilliseconds > opt.DeadlineMs) { Log("not ready in time"); Quit(3); }
                return;
            }
            bool want = Native.IsWindowVisible(opt.Page) && !Native.IsIconic(opt.Owner);
            if (want)
            {
                Native.POINT p;
                p.X = opt.X; p.Y = opt.Y;
                Native.ClientToScreen(opt.Page, ref p);
                if (!shown || p.X != last.X || p.Y != last.Y)
                {
                    uint flags = Native.SWP_NOZORDER | Native.SWP_NOACTIVATE | Native.SWP_SHOWWINDOW;
                    Native.SetWindowPos(hwnd, IntPtr.Zero, p.X, p.Y, opt.W, opt.H, flags);
                    last = p;
                }
                if (!shown)
                {
                    shown = true;
                    media.Play();
                    Log("shown at " + last.X + "," + last.Y + " " + opt.W + "x" + opt.H);
                }
                long now = clock.ElapsedMilliseconds;
                if (logPath != null && now - lastPosLog >= 1000)
                {
                    lastPosLog = now;
                    Log("position " + media.Position.TotalSeconds.ToString("0.00") + "  redraws " + renderFrames +
                        ", longest gap " + maxRenderGap + " ms");
                    renderFrames = 0;
                    maxRenderGap = 0;
                }
            }
            else if (shown)
            {
                shown = false;
                media.Pause();
                Native.ShowWindow(hwnd, Native.SW_HIDE);
                Log("hidden");
            }
        }

        private void Quit(int code)
        {
            tick.Stop();
            Application.Current.Shutdown(code);
        }

        private void Log(string s)
        {
            if (logPath == null) return;
            try { File.AppendAllText(logPath, clock.ElapsedMilliseconds.ToString("00000") + " ms  " + s + "\r\n"); }
            catch { }
        }
    }

    internal static class Program
    {
        [STAThread]
        private static int Main(string[] args)
        {
            try
            {
                Options o = Options.Parse(args);
                if (o == null) return 2;
                try
                {
                    // Setup (and a virus scanner reading every file it writes) keeps the processor busy while
                    // this plays; a little above normal keeps the picture steady. GUSAN_SLIDES_NOBOOST: tests.
                    if (Environment.GetEnvironmentVariable("GUSAN_SLIDES_NOBOOST") == null)
                        Process.GetCurrentProcess().PriorityClass = ProcessPriorityClass.AboveNormal;
                }
                catch { }
                Application app = new Application();
                return app.Run(new Host(o));
            }
            catch (Exception ex)
            {
                try
                {
                    string log = Environment.GetEnvironmentVariable("GUSAN_SLIDES_LOG");
                    if (log != null) File.AppendAllText(log, "crash: " + ex + "\r\n");
                }
                catch { }
                return 1;
            }
        }
    }
}
