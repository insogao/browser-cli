// Test-only native messaging host for SW-1.
//
// It speaks the Chrome native-messaging framing (4-byte little-endian length +
// UTF-8 JSON on stdin/stdout), logs every start/receive/exit with its own PID,
// and exits when stdin closes (which is what Chrome does when the extension
// port disconnects). The runner compiles this with csc.exe per run; the log
// path is patched in at compile time so no environment variables are needed.
using System;
using System.Diagnostics;
using System.IO;
using System.Text;

internal static class Sw1NativeHost
{
    private const string LogPath = "__SW1_HOST_LOG__";

    private static void Log(string message)
    {
        try
        {
            File.AppendAllText(
                LogPath,
                DateTime.UtcNow.ToString("o") + " pid=" + Process.GetCurrentProcess().Id +
                " " + message + Environment.NewLine,
                Encoding.UTF8);
        }
        catch
        {
            // Never fail the host because of logging.
        }
    }

    private static bool ReadExact(Stream stream, byte[] buffer, int count)
    {
        int offset = 0;
        while (offset < count)
        {
            int read = stream.Read(buffer, offset, count - offset);
            if (read <= 0) return false;
            offset += read;
        }
        return true;
    }

    private static void Send(Stream stdout, string json)
    {
        byte[] payload = Encoding.UTF8.GetBytes(json);
        byte[] header = BitConverter.GetBytes(payload.Length);
        if (!BitConverter.IsLittleEndian) Array.Reverse(header);
        stdout.Write(header, 0, 4);
        stdout.Write(payload, 0, payload.Length);
        stdout.Flush();
    }

    public static int Main(string[] args)
    {
        int pid = Process.GetCurrentProcess().Id;
        Log("start argv=" + string.Join(" ", args));
        using (Stream stdin = Console.OpenStandardInput())
        using (Stream stdout = Console.OpenStandardOutput())
        {
            Send(stdout, "{\"type\":\"event\",\"name\":\"ready\",\"args\":{\"pid\":" + pid + "}}");
            byte[] header = new byte[4];
            while (true)
            {
                if (!ReadExact(stdin, header, 4)) break;
                int length = BitConverter.ToInt32(header, 0);
                if (!BitConverter.IsLittleEndian)
                {
                    byte[] copy = (byte[])header.Clone();
                    Array.Reverse(copy);
                    length = BitConverter.ToInt32(copy, 0);
                }
                if (length <= 0 || length > 16 * 1024 * 1024) break;
                byte[] payload = new byte[length];
                if (!ReadExact(stdin, payload, length)) break;
                string message = Encoding.UTF8.GetString(payload);
                Log("recv " + message);
                string echo = message.Replace("\\", "\\\\").Replace("\"", "\\\"");
                Send(stdout, "{\"type\":\"result\",\"requestId\":\"sw1\",\"ok\":true," +
                             "\"data\":{\"pid\":" + pid + ",\"echo\":\"" + echo + "\"}}");
            }
            Log("stdin-eof-exit");
        }
        return 0;
    }
}
