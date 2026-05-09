namespace Loupedeck.DemoPlugin
{
    using System;
    using System.IO;
    using System.Numerics;
    using SixLabors.Fonts;
    using SixLabors.ImageSharp;
    using SixLabors.ImageSharp.Drawing.Processing;
    using SixLabors.ImageSharp.PixelFormats;
    using SixLabors.ImageSharp.Processing;

    internal static class SkillCountdownImageComposer
    {
        private static readonly Font _font;

        static SkillCountdownImageComposer()
        {
            Font font = null;
            try
            {
                var fc = new FontCollection();
                var paths = OperatingSystem.IsMacOS()
                    ? new[]
                    {
                        "/Library/Fonts/Arial.ttf",
                        "/System/Library/Fonts/Supplemental/Arial.ttf",
                        "/System/Library/Fonts/Helvetica.ttc",
                    }
                    : new[] { @"C:\Windows\Fonts\arial.ttf" };

                foreach (var path in paths)
                {
                    if (!File.Exists(path))
                    {
                        continue;
                    }

                    var family = fc.Add(path);
                    font = family.CreateFont(22f, FontStyle.Bold);
                    break;
                }
            }
            catch
            {
                font = null;
            }

            _font = font;
        }

        public static BitmapImage TryBuild(
            Int32 timerId,
            Byte[] characterPngBytes,
            Byte[] topSkillPngBytes,
            Byte[] bottomSkillPngBytes,
            Int32 flashSeconds,
            Boolean flashRunning,
            Int32 teleportSeconds,
            Boolean teleportRunning,
            String signalOverlayKey,
            Boolean? allyChannelActive)
        {
            Image<Rgba32> character;
            try
            {
                character = Image.Load<Rgba32>(characterPngBytes);
            }
            catch
            {
                return null;
            }

            using (character)
            {
                using var image = new Image<Rgba32>(100, 100, new Rgba32(0, 0, 0, 0));
                DrawLayer(image, character, new Rectangle(0, 0, 70, 100));
                DrawLayer(image, topSkillPngBytes, new Rectangle(70, 0, 30, 50));
                DrawLayer(image, bottomSkillPngBytes, new Rectangle(70, 50, 30, 50));

                if (timerId >= 1 && timerId <= 5)
                {
                    // Slight red tint on the character region for timers 1-5.
                    image.Mutate(ctx => ctx.Fill(Color.ParseHex("#80FF0000"), new RectangleF(0, 0, 70, 100)));
                }

                DrawOverlayOnSlot(image, new RectangleF(70, 0, 30, 50), flashSeconds, flashRunning);
                DrawOverlayOnSlot(image, new RectangleF(70, 50, 30, 50), teleportSeconds, teleportRunning);

                if (timerId == 1 && !String.Equals(signalOverlayKey, "idle", StringComparison.OrdinalIgnoreCase))
                {
                    DrawTimer1SignalCorners(image, signalOverlayKey);
                }

                try
                {
                    var bgColor = allyChannelActive.HasValue
                        ? (allyChannelActive.Value ? new Rgba32(0, 170, 70, 255) : new Rgba32(0, 95, 200, 255))
                        : (timerId >= 1 && timerId <= 5
                            ? new Rgba32(180, 20, 20, 255)
                            : new Rgba32(0, 0, 0, 0));
                    using var framed = ApplyInsetScale(image, 0.95f, bgColor);
                    using var ms = new MemoryStream();
                    framed.SaveAsPng(ms);
                    var bytes = ms.ToArray();
                    return BitmapImage.TryCreateFromArray(bytes, out var bmp) ? bmp : null;
                }
                catch
                {
                    return null;
                }
            }
        }

        private static void DrawOverlayOnSlot(Image<Rgba32> image, RectangleF slot, Int32 seconds, Boolean isRunning)
        {
            if (_font == null)
            {
                return;
            }

            var text = isRunning && seconds > 0 ? seconds.ToString() : "?";
            var textOptions = new TextOptions(_font);
            var bounds = TextMeasurer.MeasureBounds(text, textOptions);
            var x = slot.X + (slot.Width - bounds.Width) / 2f - bounds.Left;
            var y = slot.Y + (slot.Height - bounds.Height) / 2f - bounds.Top;
            var padX = 3f;
            var padY = 1f;
            var badge = new RectangleF(
                slot.X + (slot.Width - (bounds.Width + padX * 2f)) / 2f,
                slot.Y + (slot.Height - (bounds.Height + padY * 2f)) / 2f,
                bounds.Width + padX * 2f,
                bounds.Height + padY * 2f);

            image.Mutate(ctx =>
            {
                // Draw a small white badge only behind timer text.
                ctx.Fill(Color.ParseHex("#CCFFFFFF"), badge);
                ctx.DrawText(text, _font, Color.Black, new Vector2(x, y));
            });
        }

        private static void DrawTimer1SignalCorners(Image<Rgba32> image, String overlayKey)
        {
            const Int32 d = 5;
            if (overlayKey.Contains("ur_y", StringComparison.Ordinal))
            {
                FillCorner(image, 100 - d, 0, d, d, Color.ParseHex("#FFFF00"));
            }
            else if (overlayKey.Contains("ur_p", StringComparison.Ordinal))
            {
                FillCorner(image, 100 - d, 0, d, d, Color.ParseHex("#800080"));
            }

            if (overlayKey.Contains("br_g", StringComparison.Ordinal))
            {
                FillCorner(image, 100 - d, 100 - d, d, d, Color.ParseHex("#00FF00"));
            }
            else if (overlayKey.Contains("br_r", StringComparison.Ordinal))
            {
                FillCorner(image, 100 - d, 100 - d, d, d, Color.ParseHex("#FF0000"));
            }
        }

        private static void FillCorner(Image<Rgba32> image, Single x, Single y, Single w, Single h, Color color)
        {
            image.Mutate(ctx => ctx.Fill(color, new RectangleF(x, y, w, h)));
        }

        private static void DrawLayer(Image<Rgba32> target, Byte[] pngBytes, Rectangle area)
        {
            if (pngBytes == null || pngBytes.Length == 0)
            {
                return;
            }

            try
            {
                using var src = Image.Load<Rgba32>(pngBytes);
                DrawLayer(target, src, area);
            }
            catch
            {
            }
        }

        private static void DrawLayer(Image<Rgba32> target, Image<Rgba32> src, Rectangle area)
        {
            using var resized = src.Clone(ctx => ctx.Resize(new ResizeOptions
            {
                Size = new Size(area.Width, area.Height),
                Mode = ResizeMode.Stretch,
            }));
            target.Mutate(ctx => ctx.DrawImage(resized, new Point(area.X, area.Y), 1f));
        }

        private static Image<Rgba32> ApplyInsetScale(Image<Rgba32> source, Single scale, Rgba32 backgroundColor)
        {
            var width = source.Width;
            var height = source.Height;
            var scaledWidth = Math.Max(1, (Int32)Math.Round(width * scale));
            var scaledHeight = Math.Max(1, (Int32)Math.Round(height * scale));
            var offsetX = (width - scaledWidth) / 2;
            var offsetY = (height - scaledHeight) / 2;

            using var scaled = source.Clone(ctx => ctx.Resize(scaledWidth, scaledHeight));
            var framed = new Image<Rgba32>(width, height, backgroundColor);
            framed.Mutate(ctx => ctx.DrawImage(scaled, new Point(offsetX, offsetY), 1f));
            return framed;
        }
    }
}
