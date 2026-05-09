namespace Loupedeck.DemoPlugin
{
    using System;
    using System.Collections.Generic;
    using System.Net.Sockets;
    using System.Text;
    using System.Timers;

    /// <summary>
    /// Base class for the 5 enemy hero buttons on the Creative Console.
    /// Each button shows the enemy hero image and a countdown overlay
    /// when a summoner spell cooldown is triggered (e.g. TP, Flash).
    /// </summary>
    public abstract class CountdownTimerCommandBase : PluginDynamicCommand
    {
        private const Int32 LocalIpcPort = 5006;
        private const Int32 AllyTimerStart = 6;
        private const Int32 AllyTimerEnd = 9;
        private static readonly Timer IconRefreshTimer;
        private static event Action RefreshTick;
        private static readonly String[] CharacterNamesByTimerId =
        {
            "蓋倫",
            "安妮",
            "好運姐",
            "阿姆姆",
            "雷歐娜",
            "墨菲特",
            "馬爾札哈",
            "艾希",
            "沃維克",
            "索娜",
        };

        static CountdownTimerCommandBase()
        {
            IconRefreshTimer = new Timer(1000);
            IconRefreshTimer.AutoReset = true;
            IconRefreshTimer.Elapsed += (_, __) => RefreshTick?.Invoke();
            IconRefreshTimer.Start();
        }

        private readonly Int32 _timerId;
        private readonly Dictionary<Int32, String> _frameResources = new Dictionary<Int32, String>();
        private readonly Byte[] _characterImageBytes;
        private readonly Byte[] _topSkillImageBytes;
        private readonly Byte[] _bottomSkillImageBytes;
        private readonly UdpClient _udpClient = new UdpClient();

        protected CountdownTimerCommandBase(Int32 timerId, String displayName)
            : base(displayName: displayName, description: "閃現 / 傳送 各一組倒數", groupName: "Timers")
        {
            this._timerId = timerId;

            Byte[] characterBytes = null;
            if (timerId >= 1 && timerId <= CharacterNamesByTimerId.Length)
            {
                characterBytes = LoadResourceBytes(
                    $"{CharacterNamesByTimerId[timerId - 1]}.png",
                    $"{CharacterNamesByTimerId[timerId - 1]}_70.png");
            }

            this._characterImageBytes = characterBytes;
            this._topSkillImageBytes = LoadResourceBytes("閃現.png", "閃現.PNG");
            this._bottomSkillImageBytes = LoadResourceBytes("傳送.PNG", "傳送.png");

            for (var seconds = 0; seconds <= 10; seconds++)
            {
                var fileName = $"countdown_{seconds:00}.png";
                try
                {
                    var resourcePath = PluginResources.FindFile(fileName);
                    this._frameResources[seconds] = resourcePath;
                }
                catch
                {
                }
            }

            CountdownState.StateChanged += this.OnCountdownStateChanged;
            AllyChannelState.StateChanged += this.OnAllyChannelStateChanged;
            RefreshTick += this.OnRefreshTick;
            if (this._timerId == 1)
            {
                SignalBlockState.StateChanged += this.OnSignalStateChanged;
            }
        }



        protected override void RunCommand(String actionParameter)
        {
            if (TryGetAllySlot(this._timerId, out var allySlot))
            {
                // Ally slots are communication toggles; countdown starts only from UDP signals.
                AllyChannelState.ToggleTarget(allySlot);
                SendIpcToggle(allySlot);
                return;
            }

            CountdownState.StartCountdown(this._timerId, CountdownSkill.Flash);
        }

        protected override String GetCommandDisplayName(String actionParameter, PluginImageSize imageSize)
        {
            return String.Empty;
        }

        protected override BitmapImage GetCommandImage(String actionParameter, PluginImageSize imageSize)
        {
            var (fSec, fRun) = CountdownState.GetSnapshot(this._timerId, CountdownSkill.Flash);
            var (tSec, tRun) = CountdownState.GetSnapshot(this._timerId, CountdownSkill.Teleport);
            var overlayKey = SignalBlockState.GetOverlayKey();
            Boolean? allyChannelActive = null;
            if (TryGetAllySlot(this._timerId, out var allySlot))
            {
                allyChannelActive = AllyChannelState.IsTargeted(allySlot);
            }

            var characterBytes = this._characterImageBytes;
            var topSkillBytes = this._topSkillImageBytes;
            var bottomSkillBytes = this._bottomSkillImageBytes;
            if (LiveInfoIconMapper.TryGetSlotResources(this._timerId, out var liveChampion, out var liveSpell1, out var liveSpell2))
            {
                if (liveChampion != null)
                {
                    characterBytes = liveChampion;
                }

                if (liveSpell1 != null)
                {
                    topSkillBytes = liveSpell1;
                }

                if (liveSpell2 != null)
                {
                    bottomSkillBytes = liveSpell2;
                }
            }

            if (characterBytes != null && characterBytes.Length > 0)
            {
                var composed = SkillCountdownImageComposer.TryBuild(
                    this._timerId,
                    characterBytes,
                    topSkillBytes,
                    bottomSkillBytes,
                    fSec,
                    fRun,
                    tSec,
                    tRun,
                    overlayKey,
                    allyChannelActive);
                if (composed != null)
                {
                    return composed;
                }
            }

            var frameSec = fRun ? fSec : (tRun ? tSec : Math.Max(fSec, tSec));
            if (this._frameResources.TryGetValue(frameSec, out var resourcePath))
            {
                return PluginResources.ReadImage(resourcePath);
            }

            return null;
        }

        private static Byte[] LoadResourceBytes(params String[] candidates)
        {
            foreach (var candidate in candidates)
            {
                try
                {
                    return PluginResources.ReadBinaryFile(PluginResources.FindFile(candidate));
                }
                catch
                {
                }
            }

            return null;
        }

        private void SendIpcToggle(Int32 slotId)
        {
            try
            {
                var msg = $"PTT_ALLY{slotId}_TOGGLE";
                var data = Encoding.UTF8.GetBytes(msg);
                this._udpClient.Send(data, data.Length, "127.0.0.1", LocalIpcPort);
            }
            catch
            {
                // UI toggle still works without local IPC.
            }
        }

        private void OnCountdownStateChanged(Int32 changedTimerId)
        {
            if (changedTimerId == this._timerId)
            {
                this.ActionImageChanged();
            }
        }

        private void OnAllyChannelStateChanged(Int32 changedSlot)
        {
            if (TryGetAllySlot(this._timerId, out var allySlot)
                && (changedSlot == 0 || changedSlot == allySlot))
            {
                this.ActionImageChanged();
            }
        }

        private static Boolean TryGetAllySlot(Int32 timerId, out Int32 allySlot)
        {
            if (timerId >= AllyTimerStart && timerId <= AllyTimerEnd)
            {
                allySlot = timerId - AllyTimerStart + 1; // timer 6..9 -> ally slot 1..4
                return true;
            }

            allySlot = 0;
            return false;
        }

        private void OnSignalStateChanged()
        {
            if (this._timerId == 1)
            {
                this.ActionImageChanged();
            }
        }

        private void OnRefreshTick()
        {
            this.ActionImageChanged();
        }

    }

    public class CountdownTimer1Command : CountdownTimerCommandBase
    {
        public CountdownTimer1Command()
            : base(1, "Countdown Timer 1")
        {
        }
    }

    public class CountdownTimer2Command : CountdownTimerCommandBase
    {
        public CountdownTimer2Command()
            : base(2, "Countdown Timer 2")
        {
        }
    }

    public class CountdownTimer3Command : CountdownTimerCommandBase
    {
        public CountdownTimer3Command()
            : base(3, "Countdown Timer 3")
        {
        }
    }

    public class CountdownTimer4Command : CountdownTimerCommandBase
    {
        public CountdownTimer4Command()
            : base(4, "Countdown Timer 4")
        {
        }
    }

    public class CountdownTimer5Command : CountdownTimerCommandBase
    {
        public CountdownTimer5Command()
            : base(5, "Countdown Timer 5")
        {
        }
    }

    public class CountdownTimer6Command : CountdownTimerCommandBase
    {
        public CountdownTimer6Command()
            : base(6, "Countdown Timer 6")
        {
        }
    }

    public class CountdownTimer7Command : CountdownTimerCommandBase
    {
        public CountdownTimer7Command()
            : base(7, "Countdown Timer 7")
        {
        }
    }

    public class CountdownTimer8Command : CountdownTimerCommandBase
    {
        public CountdownTimer8Command()
            : base(8, "Countdown Timer 8")
        {
        }
    }

    public class CountdownTimer9Command : CountdownTimerCommandBase
    {
        public CountdownTimer9Command()
            : base(9, "Countdown Timer 9")
        {
        }
    }

    public class CountdownTimer10Command : CountdownTimerCommandBase
    {
        public CountdownTimer10Command()
            : base(10, "Countdown Timer 10")
        {
        }
    }
}
