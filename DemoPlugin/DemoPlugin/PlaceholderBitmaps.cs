namespace Loupedeck.DemoPlugin
{
    using System;
    using System.IO;
    using SixLabors.ImageSharp;
    using SixLabors.ImageSharp.PixelFormats;

    internal static class PlaceholderBitmaps
    {
        /// <summary>
        /// Opaque black 100×100 PNG for Creative Console tiles until live game data is available.
        /// </summary>
        public static BitmapImage TryBlack100()
        {
            try
            {
                using var img = new Image<Rgba32>(100, 100, new Rgba32(0, 0, 0, 255));
                using var ms = new MemoryStream();
                img.SaveAsPng(ms);
                var bytes = ms.ToArray();
                return BitmapImage.TryCreateFromArray(bytes, out var bmp) ? bmp : null;
            }
            catch
            {
                return null;
            }
        }
    }
}
