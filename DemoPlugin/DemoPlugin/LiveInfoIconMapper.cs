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
                    Path.Combine(root, "characters", $"{imageBaseName}.png"),
                    Path.Combine(root, "characters", $"{imageBaseName}.PNG"),
                    Path.Combine(root, "skills", $"{imageBaseName}.png"),
                    Path.Combine(root, "skills", $"{imageBaseName}.PNG"),
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
            var baseDir = AppContext.BaseDirectory;
            var candidates = new List<String>
            {
                Path.Combine(baseDir, "images", "lol_character", "info", "lol_live_info.json"),
                Path.Combine(baseDir, "DemoPlugin", "images", "lol_character", "info", "lol_live_info.json"),
            };

            var cursor = new DirectoryInfo(baseDir);
            for (var i = 0; i < 10 && cursor != null; i++)
            {
                candidates.Add(Path.Combine(cursor.FullName, "images", "lol_character", "info", "lol_live_info.json"));
                candidates.Add(Path.Combine(cursor.FullName, "DemoPlugin", "images", "lol_character", "info", "lol_live_info.json"));
                cursor = cursor.Parent;
            }

            candidates.Add(Path.Combine("/Users/caesar/Desktop/actions-sdk", "DemoPlugin", "DemoPlugin", "images", "lol_character", "info", "lol_live_info.json"));

            foreach (var c in candidates)
            {
                if (File.Exists(c))
                {
                    return c;
                }
            }

            return String.Empty;
        }
    }
}
