namespace Loupedeck.DemoPlugin
{
    using System;
    using System.Net.Sockets;
    using System.Text;

    /// <summary>
    /// When a tracked enemy countdown (UDP START…) reaches zero, sends one UTF-8 line to
    /// pip_message_overlay.py (same UDP format as overlay_udp.send_overlay_line).
    /// </summary>
    internal static class CooldownEndOverlayNotifier
    {
        private static Boolean _hooked;

        public static void Initialize()
        {
            if (_hooked)
            {
                return;
            }

            _hooked = true;
            CountdownState.CooldownEnded += OnCooldownEnded;
        }

        private static Boolean NotifyEnabled()
        {
            var flag = Environment.GetEnvironmentVariable("LOL_PIP_CD_NOTIFY");
            if (String.IsNullOrWhiteSpace(flag))
            {
                return true;
            }

            var l = flag.Trim().ToLowerInvariant();
            return l is not ("0" or "false" or "no" or "off");
        }

        private static void OnCooldownEnded(Int32 timerId, CountdownSkill skill)
        {
            if (!NotifyEnabled())
            {
                return;
            }

            // Only enemy row timers — ally countdown UX not used for CD strip today.
            if (timerId < 1 || timerId > 5)
            {
                return;
            }

            var line = LiveInfoIconMapper.TryFormatEnemyCooldownReadyLine(timerId, skill, out var formatted)
                ? formatted
                : (skill == CountdownSkill.Flash
                    ? $"敵方 T{timerId} · 閃現 已恢復"
                    : $"敵方 T{timerId} · 傳送 已恢復");

            TrySendUdp(line);
        }

        private static void TrySendUdp(String line)
        {
            var host = Environment.GetEnvironmentVariable("LOL_PIP_OVERLAY_HOST");
            if (String.IsNullOrWhiteSpace(host))
            {
                host = "127.0.0.1";
            }
            else
            {
                host = host.Trim();
            }

            var port = 5011;
            var portStr = Environment.GetEnvironmentVariable("LOL_PIP_OVERLAY_PORT");
            if (!String.IsNullOrWhiteSpace(portStr)
                && Int32.TryParse(portStr.Trim(), out var parsed)
                && parsed > 0
                && parsed <= 65535)
            {
                port = parsed;
            }

            try
            {
                var nl = line.Replace("\r\n", "\n").Replace("\r", "\n").TrimEnd('\n') + "\n";
                var bytes = Encoding.UTF8.GetBytes(nl);
                if (bytes.Length > 65000)
                {
                    return;
                }

                using var udp = new UdpClient();
                udp.Send(bytes, bytes.Length, host, port);
            }
            catch (Exception ex)
            {
                PluginLog.Warning(ex, "Cooldown-end PiP overlay UDP send failed");
            }
        }
    }
}
