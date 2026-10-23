Sims 1 Legacy HD
================

For The Sims Legacy Collection (the 2025 re-release of The Sims 1, Steam). It makes the
game sharp on high-resolution screens, at your monitor's own resolution:

- The interface (windows, panels, text) is drawn at screen resolution instead of being
  blown up from a small picture, with sharp fonts. Icons are smoothed.
- Objects, walls, floors, the Sims' clothes and skin, and the Sims' portraits are shown in
  high definition (redrawn at three times the detail).
- Some of the game's own drawing glitches in a lot are fixed (light dots between floors and
  along wall tops, specks along the edges of objects).

The mod changes no file of the game: it adds vulkan-1.dll next to Sims.exe and a vkproxy
folder.


What you need
-------------

- The Sims Legacy Collection on Steam, on Windows 10 or 11.
- A screen of 1440p or more. The game enlarges its picture by whole steps of its height
  (600 pixels): 1440p and 1600p by 2, 2160p (4K) by 3, 2880p (5K) by 4. At 1080p the game
  draws everything at its own size and the mod has nothing to add.


Install
-------

1. Find the game folder: in Steam, right-click The Sims Legacy Collection > Manage >
   Browse local files (the folder with Sims.exe).
2. Extract the "core" archive into that folder, then any of the other parts you want, the
   same way. Each part works on its own (with core):
     core        required: the mod itself, fonts
     objects     furniture and every other object, the Buy and Build mode pictures
     walls       wallpapers, wall tops, floors
     sims        the Sims' clothes and skin
     portraits   the Sims' portraits (the families the game comes with, and every head for
                 Sims you make)
3. Start the game. In Options > Graphics, set Resolution to your monitor's own resolution
   (for example 3840x2160 on a 4K screen); it takes effect when the game starts again.

To uninstall, delete vulkan-1.dll and the vkproxy folder from the game folder.


Good to know
------------

- Objects behind walls and fences can flash for a moment while the view scrolls: the game
  does that without the mod too.
- The first visit to a lot reads the HD files once; later camera turns reuse them.
- If something goes wrong, the file %TEMP%\vkproxy\vkproxy.log says what the mod found
  and any error; please include it when you report a problem.


This is a fan-made mod, not made or endorsed by Electronic Arts or Maxis. The HD pictures
are made from the game's own art; you need your own copy of the game.
