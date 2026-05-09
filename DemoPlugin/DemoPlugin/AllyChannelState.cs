namespace Loupedeck.DemoPlugin
{
    using System;
    using System.Timers;

    /// <summary>
    /// Manages ally channel communication state (multi-select).
    /// Default: all channels enabled.
    /// Presses are debounced in a 0.5s selection window.
    /// </summary>
    internal static class AllyChannelState
    {
        public const Int32 AllySlotCount = 4;  // 5v5: 4 allies
        public const Int32 EnemySlotCount = 5;  // 5 enemies
        private const Double SelectionWindowMs = 500;

        private static readonly Object LockObject = new Object();
        private static readonly Timer CommitTimer;

        // Slot index (0-3) → role name ("JG", "MID", …). Empty string = unassigned.
        private static readonly String[] AllyRoles = new String[AllySlotCount];

        // Current committed communication state (true = green / receives voice).
        private static readonly Boolean[] ActiveChannels = new Boolean[AllySlotCount];
        // Pending state during the 0.5 second press window.
        private static readonly Boolean[] PendingChannels = new Boolean[AllySlotCount];
        private static Boolean HasPendingSelection;

        // Enemy slots (0-4) → hero name.
        private static readonly String[] EnemyHeroes = new String[EnemySlotCount];

        // My own role + hero.
        private static String _myRole = "";
        private static String _myHero = "";

        static AllyChannelState()
        {
            CommitTimer = new Timer(SelectionWindowMs);
            CommitTimer.AutoReset = false;
            CommitTimer.Elapsed += (_, __) => CommitPendingSelection();

            for (var i = 0; i < AllySlotCount; i++)
            {
                AllyRoles[i] = "";
                ActiveChannels[i] = true;
                PendingChannels[i] = true;
            }
            for (var i = 0; i < EnemySlotCount; i++)
                EnemyHeroes[i] = "";
        }

        /// <summary>Fired when target or config changes. Param = changed slot (1-based), or 0 for global.</summary>
        public static event Action<Int32> StateChanged;

        // ── My info ────────────────────────────────────────────────

        public static void SetMyRole(String role)
        {
            lock (LockObject) { _myRole = role ?? ""; }
            RaiseStateChanged(0);
        }

        public static String GetMyRole()
        {
            lock (LockObject) { return _myRole; }
        }

        public static void SetMyHero(String hero)
        {
            lock (LockObject) { _myHero = hero ?? ""; }
        }

        public static String GetMyHero()
        {
            lock (LockObject) { return _myHero; }
        }

        // ── Ally config ────────────────────────────────────────────

        public static void SetAllyRole(Int32 slot, String role)
        {
            var idx = ValidateSlot(slot, AllySlotCount);
            lock (LockObject) { AllyRoles[idx] = role ?? ""; }
            RaiseStateChanged(slot);
        }

        public static String GetAllyRole(Int32 slot)
        {
            var idx = ValidateSlot(slot, AllySlotCount);
            lock (LockObject) { return AllyRoles[idx]; }
        }

        // ── Enemy config ───────────────────────────────────────────

        public static void SetEnemyHero(Int32 slot, String heroName)
        {
            var idx = ValidateSlot(slot, EnemySlotCount);
            lock (LockObject) { EnemyHeroes[idx] = heroName ?? ""; }
        }

        public static String GetEnemyHero(Int32 slot)
        {
            var idx = ValidateSlot(slot, EnemySlotCount);
            lock (LockObject) { return EnemyHeroes[idx]; }
        }

        // ── Ally channel communication set (multi target) ───────────

        /// <summary>
        /// Toggle communication for a specific ally slot.
        /// Selection is committed after 0.5s; multiple presses in the window are grouped.
        /// </summary>
        public static void ToggleTarget(Int32 slot)
        {
            var idx = ValidateSlot(slot, AllySlotCount);
            lock (LockObject)
            {
                if (!HasPendingSelection)
                {
                    var allGreen = true;
                    for (var i = 0; i < AllySlotCount; i++)
                    {
                        if (!ActiveChannels[i])
                        {
                            allGreen = false;
                            break;
                        }
                    }

                    if (allGreen)
                    {
                        // Requirement: when all are green, first press starts selection mode.
                        // Start from all blue, then pressed allies become green during 0.5s window.
                        for (var i = 0; i < AllySlotCount; i++)
                        {
                            PendingChannels[i] = false;
                        }
                    }
                    else
                    {
                        // Normal mode: start from current committed state.
                        Array.Copy(ActiveChannels, PendingChannels, AllySlotCount);
                    }

                    HasPendingSelection = true;
                }

                PendingChannels[idx] = !PendingChannels[idx];
                CommitTimer.Stop();
                CommitTimer.Start();
            }
            RaiseStateChanged(0); // Refresh all buttons immediately.
        }

        /// <summary>Force broadcast mode (ALL).</summary>
        public static void ClearTarget()
        {
            lock (LockObject)
            {
                for (var i = 0; i < AllySlotCount; i++)
                {
                    ActiveChannels[i] = true;
                    PendingChannels[i] = true;
                }

                HasPendingSelection = false;
                CommitTimer.Stop();
            }
            RaiseStateChanged(0);
        }

        /// <summary>
        /// Current legacy target role. Empty string means ALL (or multi-target) for compatibility.
        /// </summary>
        public static String GetCurrentTarget()
        {
            return String.Empty;
        }

        /// <summary>True = green (receives voice), false = blue (muted).</summary>
        public static Boolean IsTargeted(Int32 slot)
        {
            var idx = ValidateSlot(slot, AllySlotCount);
            lock (LockObject)
            {
                return HasPendingSelection ? PendingChannels[idx] : ActiveChannels[idx];
            }
        }

        // ── Helpers ────────────────────────────────────────────────

        private static void CommitPendingSelection()
        {
            lock (LockObject)
            {
                if (!HasPendingSelection)
                {
                    return;
                }

                Array.Copy(PendingChannels, ActiveChannels, AllySlotCount);
                HasPendingSelection = false;

                // Requirement: if all channels are closed, auto-reset to ALL enabled.
                var anyEnabled = false;
                for (var i = 0; i < AllySlotCount; i++)
                {
                    if (ActiveChannels[i])
                    {
                        anyEnabled = true;
                        break;
                    }
                }

                if (!anyEnabled)
                {
                    for (var i = 0; i < AllySlotCount; i++)
                    {
                        ActiveChannels[i] = true;
                        PendingChannels[i] = true;
                    }
                }
            }

            RaiseStateChanged(0);
        }

        private static Int32 ValidateSlot(Int32 slot, Int32 max)
        {
            if (slot < 1 || slot > max)
                throw new ArgumentOutOfRangeException(nameof(slot), $"Slot must be 1-{max}");
            return slot - 1;
        }

        private static void RaiseStateChanged(Int32 slot)
            => StateChanged?.Invoke(slot);
    }
}
