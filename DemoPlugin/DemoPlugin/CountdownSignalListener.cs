namespace Loupedeck.DemoPlugin
{
    using System;
    using System.Net;
    using System.Net.Sockets;
    using System.Text;
    using System.Text.RegularExpressions;
    using System.Threading;
    using System.Threading.Tasks;

    internal static class CountdownSignalListener
    {
        private const Int32 UdpPort = 5005;
        private const Int32 MaxTimerId = 5;  // 5v5: 5 enemies

        private static readonly Object LockObject = new Object();

        private static UdpClient _udpClient;
        private static CancellationTokenSource _cts;
        private static Task _listenTask;

        public static void Start()
        {
            lock (LockObject)
            {
                if (_listenTask != null)
                {
                    return;
                }

                _cts = new CancellationTokenSource();
                _udpClient = new UdpClient(UdpPort);
                _listenTask = Task.Run(() => ListenLoop(_cts.Token));
                PluginLog.Info($"Countdown signal listener started on UDP port {UdpPort}");
            }
        }

        public static void Stop()
        {
            lock (LockObject)
            {
                if (_listenTask == null)
                {
                    return;
                }

                _cts.Cancel();
                _udpClient.Close();
                _cts.Dispose();
                _udpClient = null;
                _cts = null;
                _listenTask = null;
                PluginLog.Info("Countdown signal listener stopped");
            }
        }

        private static async Task ListenLoop(CancellationToken token)
        {
            while (!token.IsCancellationRequested)
            {
                try
                {
                    var result = await _udpClient.ReceiveAsync();
                    var message = Encoding.UTF8.GetString(result.Buffer).Trim();

                    // Handle signal overlay messages (existing behavior)
                    SignalBlockState.HandleSignal(message);

                    // Ensure timer 1 always starts when legacy corner signals arrive.
                    if (message.Equals("1-1", StringComparison.OrdinalIgnoreCase))
                    {
                        CountdownState.StartCountdown(1, CountdownSkill.Flash);
                        PluginLog.Info($"Countdown T1 skill=Flash started from legacy signal ({result.RemoteEndPoint})");
                    }
                    else if (message.Equals("1-2", StringComparison.OrdinalIgnoreCase))
                    {
                        CountdownState.StartCountdown(1, CountdownSkill.Teleport);
                        PluginLog.Info($"Countdown T1 skill=Teleport started from legacy signal ({result.RemoteEndPoint})");
                    }

                    if (TryParseStartMessage(message, out var timerId, out var skill))
                    {
                        CountdownState.StartCountdown(timerId, skill);
                        PluginLog.Info($"Countdown T{timerId} skill={skill} started from UDP ({result.RemoteEndPoint})");
                    }
                }
                catch (ObjectDisposedException)
                {
                    break;
                }
                catch (Exception ex)
                {
                    PluginLog.Warning(ex, "Countdown signal listener error");
                }
            }
        }

        /// <summary>
        /// <c>START</c> defaults to timer 1 flash.<br/>
        /// <c>START5</c> starts flash only; <c>START5T</c> / <c>START5TP</c> teleport; <c>START5F</c> explicitly flash.</summary>
        internal static Boolean TryParseStartMessage(String message, out Int32 timerId, out CountdownSkill skill)
        {
            timerId = 1;
            skill = CountdownSkill.Flash;
            if (String.IsNullOrWhiteSpace(message))
            {
                return false;
            }

            // Backward compatibility: accept plain numeric signals like "1", "5T", "3F".
            if (TryParseNumericOnlyMessage(message.Trim(), out timerId, out skill))
            {
                return timerId >= 1 && timerId <= MaxTimerId;
            }

            if (!message.StartsWith("START", StringComparison.OrdinalIgnoreCase))
            {
                return false;
            }

            var rest = message.Substring(5).Trim();
            if (String.IsNullOrEmpty(rest))
            {
                return true;
            }

            ParseTimerIdAndSkill(rest, out timerId, out skill);
            if (timerId >= 1 && timerId <= MaxTimerId)
            {
                return true;
            }

            return TryParseEmbeddedPattern(message, out timerId, out skill);
        }

        private static Boolean TryParseNumericOnlyMessage(String message, out Int32 timerId, out CountdownSkill skill)
        {
            skill = CountdownSkill.Flash;
            var working = message.Trim();
            if (working.Length == 0)
            {
                timerId = 0;
                return false;
            }

            // Compatibility: "N-1" => flash, "N-2" => teleport
            var dashIndex = working.IndexOf('-', StringComparison.Ordinal);
            if (dashIndex > 0 && dashIndex < working.Length - 1)
            {
                var left = working[..dashIndex].Trim();
                var right = working[(dashIndex + 1)..].Trim();
                if (Int32.TryParse(left, out timerId))
                {
                    if (right == "1")
                    {
                        skill = CountdownSkill.Flash;
                        return true;
                    }

                    if (right == "2")
                    {
                        skill = CountdownSkill.Teleport;
                        return true;
                    }
                }
            }

            if (working.EndsWith("TP", StringComparison.OrdinalIgnoreCase) && working.Length >= 3)
            {
                skill = CountdownSkill.Teleport;
                working = working[..^2].TrimEnd();
            }
            else if (working.Length >= 2)
            {
                var suffix = working[^1];
                if (suffix is 'T' or 't')
                {
                    skill = CountdownSkill.Teleport;
                    working = working[..^1].TrimEnd();
                }
                else if (suffix is 'F' or 'f')
                {
                    skill = CountdownSkill.Flash;
                    working = working[..^1].TrimEnd();
                }
            }

            return Int32.TryParse(working, out timerId);
        }

        private static Boolean TryParseEmbeddedPattern(String message, out Int32 timerId, out CountdownSkill skill)
        {
            timerId = 0;
            skill = CountdownSkill.Flash;
            if (String.IsNullOrWhiteSpace(message))
            {
                return false;
            }

            // Accept wrapped payloads, e.g. JSON/log strings containing "1-1", "6-2", "START3T".
            var m1 = Regex.Match(message, @"\b(?<id>\d{1,2})\s*-\s*(?<slot>[12])\b");
            if (m1.Success && Int32.TryParse(m1.Groups["id"].Value, out timerId))
            {
                skill = m1.Groups["slot"].Value == "2" ? CountdownSkill.Teleport : CountdownSkill.Flash;
                return timerId >= 1 && timerId <= MaxTimerId;
            }

            var m2 = Regex.Match(message, @"START\s*(?<id>\d{1,2})\s*(?<suf>TP|T|F|TELEPORT|FLASH)?", RegexOptions.IgnoreCase);
            if (m2.Success && Int32.TryParse(m2.Groups["id"].Value, out timerId))
            {
                var suf = m2.Groups["suf"].Value;
                if (suf.Equals("TP", StringComparison.OrdinalIgnoreCase)
                    || suf.Equals("T", StringComparison.OrdinalIgnoreCase)
                    || suf.Equals("TELEPORT", StringComparison.OrdinalIgnoreCase))
                {
                    skill = CountdownSkill.Teleport;
                }
                else
                {
                    skill = CountdownSkill.Flash;
                }

                return timerId >= 1 && timerId <= MaxTimerId;
            }

            return false;
        }

        private static void ParseTimerIdAndSkill(String rest, out Int32 timerId, out CountdownSkill skill)
        {
            skill = CountdownSkill.Flash;
            var working = rest.Trim();

            if (working.EndsWith("TELEPORT", StringComparison.OrdinalIgnoreCase))
            {
                skill = CountdownSkill.Teleport;
                working = working[..^8].TrimEnd();
            }
            else if (working.EndsWith("FLASH", StringComparison.OrdinalIgnoreCase))
            {
                skill = CountdownSkill.Flash;
                working = working[..^5].TrimEnd();
            }
            else if (working.EndsWith("TP", StringComparison.OrdinalIgnoreCase) && working.Length >= 4)
            {
                // e.g. START5TP — avoid matching a lone "TP" with no id
                skill = CountdownSkill.Teleport;
                working = working[..^2].TrimEnd();
            }
            else if (working.Length >= 2)
            {
                var last = working[^1];
                if (last is 'T' or 't')
                {
                    skill = CountdownSkill.Teleport;
                    working = working[..^1].TrimEnd();
                }
                else if (last is 'F' or 'f')
                {
                    skill = CountdownSkill.Flash;
                    working = working[..^1].TrimEnd();
                }
            }

            if (String.IsNullOrEmpty(working))
            {
                timerId = 1;
                return;
            }

            if (!Int32.TryParse(working, out timerId))
            {
                timerId = 0;
            }
        }
    }
}
