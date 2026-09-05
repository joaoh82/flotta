/**
 * How often the app re-reads something that is still changing.
 *
 * Seconds, not sub-second. Provisioning is an app, a volume, a machine and a
 * boot — minutes of work — so a faster tick would not learn anything sooner,
 * and every tick is a request to a control plane somebody pays for.
 *
 * One constant rather than one per caller: the fleet list and a box's timeline
 * are watching the same event from two angles, and two numbers that must agree
 * eventually stop agreeing.
 */
export const POLL_MS = 5000;
