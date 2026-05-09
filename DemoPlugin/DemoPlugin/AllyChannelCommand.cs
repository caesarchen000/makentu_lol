namespace Loupedeck.DemoPlugin
{
    using System;
    using System.IO;
    using System.Net.Sockets;
    using System.Numerics;
    using System.Text;
    using SixLabors.Fonts;
    using SixLabors.ImageSharp;
    using SixLabors.ImageSharp.Drawing.Processing;
    using SixLabors.ImageSharp.PixelFormats;
    using SixLabors.ImageSharp.Processing;

    /// <summary>
    /// Base class for the 4 ally channel buttons on the Creative Console.
    /// Pressing = switch audio to that ally only. Pressing again = back to broadcast ALL.
    /// The button shows the ally's role label and highlights when targeted.
    /// </summary>
    public abstract class AllyChannelCommandBase : PluginDynamicCommand
    {
        private const Int32 LOCAL_IPC_PORT = 5006;
        private static readonly Font UiFont = ResolveFont();
        private readonly Int32 _slotId; // 1-based (1-4)
        private UdpClient _udpClient;

        protected AllyChannelCommandBase(Int32 slotId, String displayName)
            : base(displayName: displayName, description: $"Toggle voice to ally slot {slotId}", groupName: "Voice Channels")
        {
            this._slotId = slotId;
            this._udpClient = new UdpClient();
            AllyChannelState.StateChanged += this.OnStateChanged;
        }

        protected override void RunCommand(String actionParameter)
        {
            // Toggle this channel in a 0.5s grouped selection window.
            AllyChannelState.ToggleTarget(this._slotId);

            // Send IPC to Python client
            var msg = $"PTT_ALLY{this._slotId}_TOGGLE";
            var data = Encoding.UTF8.GetBytes(msg);
            this._udpClient.Send(data, data.Length, "127.0.0.1", LOCAL_IPC_PORT);
        }

        protected override String GetCommandDisplayName(String actionParameter, PluginImageSize imageSize)
        {
            return String.Empty;
        }

        protected override BitmapImage GetCommandImage(String actionParameter, PluginImageSize imageSize)
        {
            var role = AllyChannelState.GetAllyRole(this._slotId);
            var active = AllyChannelState.IsTargeted(this._slotId);
            var bg = active ? new Rgba32(0, 170, 70, 255) : new Rgba32(0, 95, 200, 255);
            var label = String.IsNullOrEmpty(role) ? $"A{this._slotId}" : role;

            try
            {
                using var canvas = new Image<Rgba32>(100, 100, bg);
                canvas.Mutate(ctx =>
                {
                    // subtle border for readability on hardware
                    ctx.Draw(Color.Black.WithAlpha(0.35f), 2, new RectangleF(1, 1, 98, 98));
                    if (UiFont != null)
                    {
                        var textOptions = new TextOptions(UiFont);
                        var bounds = TextMeasurer.MeasureBounds(label, textOptions);
                        var x = (100 - bounds.Width) / 2f - bounds.Left;
                        var y = (100 - bounds.Height) / 2f - bounds.Top;
                        ctx.DrawText(label, UiFont, Color.White, new Vector2(x, y));
                    }
                });

                using var ms = new MemoryStream();
                canvas.SaveAsPng(ms);
                var bytes = ms.ToArray();
                return BitmapImage.TryCreateFromArray(bytes, out var bmp) ? bmp : null;
            }
            catch
            {
                return null;
            }
        }

        private static Font ResolveFont()
        {
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
                    return family.CreateFont(20f, FontStyle.Bold);
                }
            }

            catch
            {
            }

            return null;
        }

        private void OnStateChanged(Int32 changedSlot)
        {
            // Refresh when any slot changes (since single-target affects all buttons)
            this.ActionImageChanged();
        }
    }

    // ── 4 concrete subclasses (one per ally) ────────────────────

    public class AllyChannel1Command : AllyChannelCommandBase
    {
        public AllyChannel1Command() : base(1, "Ally Channel 1") { }
    }

    public class AllyChannel2Command : AllyChannelCommandBase
    {
        public AllyChannel2Command() : base(2, "Ally Channel 2") { }
    }

    public class AllyChannel3Command : AllyChannelCommandBase
    {
        public AllyChannel3Command() : base(3, "Ally Channel 3") { }
    }

    public class AllyChannel4Command : AllyChannelCommandBase
    {
        public AllyChannel4Command() : base(4, "Ally Channel 4") { }
    }
}
