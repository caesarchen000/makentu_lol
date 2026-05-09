namespace Loupedeck.DemoPlugin
{
    using System;
    using System.Timers;

    internal static class CountdownState
    {
        private const Int32 StartSeconds = 10;
        private const Int32 TimerCount = 10;
        private const Int32 SkillSlotCount = 2;

        private static readonly Object LockObject = new Object();
        private static readonly Timer[,] Timers;

        private static readonly Int32[,] RemainingSeconds = new Int32[TimerCount, SkillSlotCount];
        private static readonly Boolean[,] IsRunning = new Boolean[TimerCount, SkillSlotCount];

        static CountdownState()
        {
            Timers = new Timer[TimerCount, SkillSlotCount];
            for (var ti = 0; ti < TimerCount; ti++)
            {
                for (var sk = 0; sk < SkillSlotCount; sk++)
                {
                    RemainingSeconds[ti, sk] = StartSeconds;
                    var capturedTimerId = ti + 1;
                    var capturedSkill = (CountdownSkill)sk;
                    var timer = new Timer(1000);
                    timer.AutoReset = true;
                    timer.Elapsed += (_, __) => OnTimerElapsed(capturedTimerId, capturedSkill);
                    Timers[ti, sk] = timer;
                }
            }
        }

        /// <summary>Fired when any skill on this timer slot changes.</summary>
        public static event Action<Int32> StateChanged;

        public static void StartCountdown(Int32 timerId, CountdownSkill skill)
        {
            var (ti, sk) = Validate(timerId, skill);
            lock (LockObject)
            {
                RemainingSeconds[ti, sk] = StartSeconds;
                IsRunning[ti, sk] = true;
                Timers[ti, sk].Start();
            }

            RaiseStateChanged(timerId);
        }

        public static (Int32 Seconds, Boolean IsRunning) GetSnapshot(Int32 timerId, CountdownSkill skill)
        {
            var (ti, sk) = Validate(timerId, skill);
            lock (LockObject)
            {
                return (RemainingSeconds[ti, sk], IsRunning[ti, sk]);
            }
        }

        private static void OnTimerElapsed(Int32 timerId, CountdownSkill skill)
        {
            var (ti, sk) = Validate(timerId, skill);
            lock (LockObject)
            {
                if (!IsRunning[ti, sk])
                {
                    return;
                }

                RemainingSeconds[ti, sk]--;
                if (RemainingSeconds[ti, sk] <= 0)
                {
                    RemainingSeconds[ti, sk] = 0;
                    IsRunning[ti, sk] = false;
                    Timers[ti, sk].Stop();
                }
            }

            RaiseStateChanged(timerId);
        }

        private static (Int32 Ti, Int32 Sk) Validate(Int32 timerId, CountdownSkill skill)
        {
            if (timerId < 1 || timerId > TimerCount)
            {
                throw new ArgumentOutOfRangeException(nameof(timerId), $"Timer id must be 1-{TimerCount}");
            }

            var sk = (Int32)skill;
            if (sk < 0 || sk >= SkillSlotCount)
            {
                throw new ArgumentOutOfRangeException(nameof(skill));
            }

            return (timerId - 1, sk);
        }

        private static void RaiseStateChanged(Int32 timerId)
            => StateChanged?.Invoke(timerId);
    }
}
