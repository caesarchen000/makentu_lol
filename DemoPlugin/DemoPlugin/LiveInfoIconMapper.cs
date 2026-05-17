namespace Loupedeck.DemoPlugin
{
    using System;
    using System.Collections.Generic;
    using System.IO;
    using System.Text.Json;

    internal static class LiveInfoIconMapper
    {
        private sealed class SlotVisual
        {
            public String Champion { get; set; } = String.Empty;
            public String Spell1 { get; set; } = String.Empty;
            public String Spell2 { get; set; } = String.Empty;
            public Boolean IsMe { get; set; }
        }

        public static void StartWatching()
        {
            // Kept for compatibility with existing plugin load flow.
            // Mapping is now read directly from JSON on every icon render.
        }

        /// <summary>
        /// True when mirrored lol_live_info.json exists, parses, status is In Game, and both teams have roster entries.
        /// </summary>
        public static Boolean TryIsGameUiReady()
        {
            var path = ResolveLiveInfoJsonPath();
            if (String.IsNullOrEmpty(path) || !File.Exists(path))
            {
                return false;
            }

            try
            {
                var json = File.ReadAllText(path);
                using var doc = JsonDocument.Parse(json);
                var root = doc.RootElement;
                if (!root.TryGetProperty("status", out var st) || st.ValueKind != JsonValueKind.String)
                {
                    return false;
                }

                if (!String.Equals(st.GetString(), "In Game", StringComparison.Ordinal))
                {
                    return false;
                }

                if (!root.TryGetProperty("theirTeam", out var their)
                    || their.ValueKind != JsonValueKind.Array
                    || their.GetArrayLength() < 1)
                {
                    return false;
                }

                if (!root.TryGetProperty("myTeam", out var mine)
                    || mine.ValueKind != JsonValueKind.Array
                    || mine.GetArrayLength() < 1)
                {
                    return false;
                }

                return true;
            }
            catch
            {
                return false;
            }
        }

        public static Boolean TryGetSlotResources(Int32 timerId, out Byte[] championBytes, out Byte[] spell1Bytes, out Byte[] spell2Bytes)
        {
            championBytes = null;
            spell1Bytes = null;
            spell2Bytes = null;

            if (!TryGetSlotVisual(timerId, out var visual) || visual == null)
            {
                return false;
            }

            championBytes = LoadImageBytes(visual.Champion);
            spell1Bytes = LoadImageBytes(visual.Spell1);
            spell2Bytes = LoadImageBytes(visual.Spell2);
            return championBytes != null || spell1Bytes != null || spell2Bytes != null;
        }

        /// <summary>
        /// One line for PiP overlay when an enemy spell countdown finishes (timers 1–5).
        /// Uses live champion / spell display names from lol_live_info.json when available.
        /// </summary>
        public static Boolean TryFormatEnemyCooldownReadyLine(Int32 timerId, CountdownSkill skill, out String line)
        {
            line = null;
            if (timerId < 1 || timerId > 5)
            {
                return false;
            }

            if (!TryGetSlotVisual(timerId, out var visual) || visual == null)
            {
                return false;
            }

            var champ = (visual.Champion ?? String.Empty).Trim();
            if (String.IsNullOrEmpty(champ))
            {
                return false;
            }

            String skillLabel = skill == CountdownSkill.Flash
                ? (String.IsNullOrWhiteSpace(visual.Spell1) ? "閃現" : visual.Spell1.Trim())
                : (String.IsNullOrWhiteSpace(visual.Spell2) ? "傳送" : visual.Spell2.Trim());

            line = $"敵方 {champ} · {skillLabel} 已恢復";
            return true;
        }

        private static Boolean TryGetSlotVisual(Int32 timerId, out SlotVisual visual)
        {
            visual = null;
            var path = ResolveLiveInfoJsonPath();
            if (String.IsNullOrEmpty(path) || !File.Exists(path))
            {
                return false;
            }

            try
            {
                var json = File.ReadAllText(path);
                using var doc = JsonDocument.Parse(json);
                var root = doc.RootElement;

                var their = ReadPlayers(root, "theirTeam");
                var mine = ReadPlayers(root, "myTeam");

                // 1..5 => enemies in JSON order
                if (timerId >= 1 && timerId <= 5)
                {
                    var idx = timerId - 1;
                    if (idx < their.Count)
                    {
                        visual = their[idx];
                        return true;
                    }

                    return false;
                }

                // 6..9 => allies except me in JSON order
                if (timerId >= 6 && timerId <= 9)
                {
                    var allies = new List<SlotVisual>();
                    foreach (var p in mine)
                    {
                        if (!p.IsMe)
                        {
                            allies.Add(p);
                        }
                    }

                    var idx = timerId - 6;
                    if (idx < allies.Count)
                    {
                        visual = allies[idx];
                        return true;
                    }

                    return false;
                }

                // 10 => me
                if (timerId == 10)
                {
                    foreach (var p in mine)
                    {
                        if (p.IsMe)
                        {
                            visual = p;
                            return true;
                        }
                    }

                    if (mine.Count > 0)
                    {
                        visual = mine[0];
                        return true;
                    }
                }
            }
            catch
            {
                return false;
            }

            return false;
        }

        private static List<SlotVisual> ReadPlayers(JsonElement root, String key)
        {
            var list = new List<SlotVisual>();
            if (!root.TryGetProperty(key, out var arr) || arr.ValueKind != JsonValueKind.Array)
            {
                return list;
            }

            foreach (var item in arr.EnumerateArray())
            {
                if (item.ValueKind != JsonValueKind.Object)
                {
                    continue;
                }

                list.Add(new SlotVisual
                {
                    IsMe = item.TryGetProperty("isMe", out var me) && me.ValueKind == JsonValueKind.True,
                    Champion = ReadString(item, "champion"),
                    Spell1 = ReadString(item, "spell1"),
                    Spell2 = ReadString(item, "spell2"),
                });
            }

            return list;
        }

        private static String ReadString(JsonElement obj, String key)
        {
            return obj.TryGetProperty(key, out var v) && v.ValueKind == JsonValueKind.String
                ? v.GetString() ?? String.Empty
                : String.Empty;
        }

        private static Byte[] LoadImageBytes(String imageBaseName)
        {
            if (String.IsNullOrWhiteSpace(imageBaseName))
            {
                return null;
            }

            foreach (var root in ResolveImagesRootCandidates())
            {
                var candidates = new[]
                {
                    // Mirror from lol_live_info.py: %LocalAppData%\...\LiveInfo\champion|spell\
                    Path.Combine(root, "champion", $"{imageBaseName}.png"),
                    Path.Combine(root, "champion", $"{imageBaseName}.PNG"),
                    Path.Combine(root, "spell", $"{imageBaseName}.png"),
                    Path.Combine(root, "spell", $"{imageBaseName}.PNG"),
                    Path.Combine(root, "characters", $"{imageBaseName}.png"),
                    Path.Combine(root, "characters", $"{imageBaseName}.PNG"),
                    Path.Combine(root, "skills", $"{imageBaseName}.png"),
                    Path.Combine(root, "skills", $"{imageBaseName}.PNG"),
                    Path.Combine(root, "lol_character", "info", "champion", $"{imageBaseName}.png"),
                    Path.Combine(root, "lol_character", "info", "champion", $"{imageBaseName}.PNG"),
                    Path.Combine(root, "lol_character", "info", "spell", $"{imageBaseName}.png"),
                    Path.Combine(root, "lol_character", "info", "spell", $"{imageBaseName}.PNG"),
                };

                foreach (var c in candidates)
                {
                    if (File.Exists(c))
                    {
                        return File.ReadAllBytes(c);
                    }
                }
            }

            try
            {
                return PluginResources.ReadBinaryFile(PluginResources.FindFile($"{imageBaseName}.png"));
            }
            catch
            {
                try
                {
                    return PluginResources.ReadBinaryFile(PluginResources.FindFile($"{imageBaseName}.PNG"));
                }
                catch
                {
                    return null;
                }
            }
        }

        private static List<String> ResolveImagesRootCandidates()
        {
            var roots = new List<String>();

            // Same folder lol_live_info.py mirrors into (must be tried before bundled plugin/images).
            var local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
            if (!String.IsNullOrEmpty(local))
            {
                roots.Add(Path.Combine(local, "Logi", "LogiPluginService", "LiveInfo"));
            }

            var repoDir = Environment.GetEnvironmentVariable("LOL_REPO_DIR");
            if (!String.IsNullOrEmpty(repoDir))
            {
                roots.Add(Path.Combine(repoDir, "DemoPlugin", "DemoPlugin", "images"));
            }

            var baseDir = AppContext.BaseDirectory;
            roots.Add(Path.Combine(baseDir, "images"));
            roots.Add(Path.Combine(baseDir, "DemoPlugin", "images"));

            var cursor = new DirectoryInfo(baseDir);
            for (var i = 0; i < 10 && cursor != null; i++)
            {
                roots.Add(Path.Combine(cursor.FullName, "images"));
                roots.Add(Path.Combine(cursor.FullName, "DemoPlugin", "images"));
                cursor = cursor.Parent;
            }

            // Workspace fallback for local dev runs.
            roots.Add(Path.Combine("/Users/caesar/Desktop/actions-sdk", "DemoPlugin", "DemoPlugin", "images"));

            var uniq = new HashSet<String>(StringComparer.Ordinal);
            var result = new List<String>();
            foreach (var r in roots)
            {
                if (Directory.Exists(r) && uniq.Add(r))
                {
                    result.Add(r);
                }
            }

            return result;
        }

        private static String ResolveLiveInfoJsonPath()
        {
            var candidates = new List<String>();

            var localPath = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
            if (!String.IsNullOrEmpty(localPath))
            {
                candidates.Add(Path.Combine(localPath, "Logi", "LogiPluginService", "LiveInfo", "lol_live_info.json"));
            }

            var repoDir = Environment.GetEnvironmentVariable("LOL_REPO_DIR");
            if (!String.IsNullOrEmpty(repoDir))
            {
                candidates.Add(Path.Combine(repoDir, "DemoPlugin", "DemoPlugin", "images", "lol_character", "info", "lol_live_info.json"));
            }

            var baseDir = AppContext.BaseDirectory;
            candidates.Add(Path.Combine(baseDir, "images", "lol_character", "info", "lol_live_info.json"));
            candidates.Add(Path.Combine(baseDir, "DemoPlugin", "images", "lol_character", "info", "lol_live_info.json"));

            var cursor = new DirectoryInfo(baseDir);
            for (var i = 0; i < 10 && cursor != null; i++)
            {
                candidates.Add(Path.Combine(cursor.FullName, "images", "lol_character", "info", "lol_live_info.json"));
                candidates.Add(Path.Combine(cursor.FullName, "DemoPlugin", "images", "lol_character", "info", "lol_live_info.json"));
                cursor = cursor.Parent;
            }

            candidates.Add(Path.Combine("/Users/caesar/Desktop/actions-sdk", "DemoPlugin", "DemoPlugin", "images", "lol_character", "info", "lol_live_info.json"));

            // Prefer newest file — bundled plugin copy is often stale; Python updates LiveInfo mirror.
            String best = null;
            var bestTime = DateTime.MinValue;
            foreach (var c in candidates)
            {
                if (!File.Exists(c))
                {
                    continue;
                }

                var t = File.GetLastWriteTimeUtc(c);
                if (t >= bestTime)
                {
                    bestTime = t;
                    best = c;
                }
            }

            return best ?? String.Empty;
        }
    }
}