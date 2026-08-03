# Where these came from

Both are lifted from the Dreame phone app, so the map here looks like the map
the app draws rather than a second, unfamiliar set of symbols for the same
things.

| File         | In the app                          | Size    |
| ------------ | ----------------------------------- | ------- |
| `vacuum.png` | `image/light/robot.png`             | 66x66   |
| `dock.png`   | `image/light/dock_log.png`          | 144x144 |

The app's `image/dark/robot.png` is byte-identical to the light one, so there
is no theme variant to carry.

## How the app draws them, which is what `map.js` copies

From `RobotAnimView` and `renderCharger` in the app's JavaScript bundle:

    screenMapInfo.robotWidth = 320 * screenMapInfo.width / realMapInfo.width;
    screenMapInfo.chargeWidth = screenMapInfo.robotWidth * 1.2;

    left: robotScreenPos.x - robotWidth / 2,
    top:  robotScreenPos.y - robotWidth / 2,
    transform: [{ rotate: "-" + robotAngle + "deg" }]

So: **320 mm** across - the robot's real footprint, not an arbitrary icon size -
drawn centred on the reported position and rotated by the *negative* of the
reported angle. The dock is the same but 1.2x. Both sprites face +x (right) at
0 degrees, which is the same convention the field-of-view cone uses.

Sizing this way means the vacuum covers the floor it actually covers, so a
point picked right beside a wall is visibly a point the vacuum cannot reach.

These are Dreame's artwork, vendored for interoperability with their own
device. Replaced easily if that is ever a problem: `map.js` falls back to
drawn shapes whenever a sprite fails to load.
