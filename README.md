# OM3D Asset Prep

A Blender add-on that takes a game asset from "imported and messy" to "baked and exported" the same way every time. It checks the scene for problems and fixes the safe ones, pairs your low and high poly meshes for baking, and exports one FBX per asset with presets for Unreal, Substance Painter and Marmoset Toolbag.

Made by Aunmar at [OnMars3D](https://onmars3d.com). Built with Claude.

## Requirements

- Blender 4.2 or newer (developed and tested on Blender 5.2)

## Install

1. Download the latest zip from the Releases page.
2. In Blender go to Edit > Preferences > Get Extensions, open the menu at the top right and choose Install from Disk.
3. Pick the zip. The panels show up in the 3D Viewport sidebar (N) under the OM3D tab: Scene Prep, Bake Prep and Batch Export.

## Naming convention

Bake Prep and Batch Export work from object names, so the add-on uses one convention throughout:

```
SM_<Asset>_<Part>_low
SM_<Asset>_<Part>_high
SM_<Asset>_<Part>_high_<Tag>     (extra highs for the same low, e.g. _high_Bolts)
```

`<Part>` is optional, so a one-piece prop can be `SM_Crate_low`. The prefix, separator and suffixes can be changed in the add-on preferences. Unreal collision meshes (`UCX_`, `UBX_`, `USP_`, `UCP_`) follow Unreal's own rule and are left alone.

## Scene Prep

Finds the problems that usually turn up in imported or inherited scenes and lists them in the panel. Pick a scope (Selected, Visible or All) and click Check Scene. Nothing changes in Check mode.

What it checks:

- **Transforms:** negative scale, non-uniform scale, unapplied rotation and scale
- **Mesh:** n-gons, non-manifold geometry, flipped normals, collapsed faces, overlapping faces, overlapping verts, T-verts, loose verts and edges
- **UVs:** missing UV map, more than one UV map, overlapping UVs (counted per material), flipped (mirrored) UVs
- **Names:** names off the convention, import junk names like `pCube3` or `polySurface12`, `.001` duplicates, mesh data names that don't match
- **Hierarchy and materials:** group empties, empty material slots, unused materials

How the list works:

- Errors, warnings and info items have their own icons. Hover an issue for a short explanation, or open the Legend.
- Problem verts light up in green in the viewport (Overlapping UVs also fills its faces while its row is open). Open one issue to see only its highlight. The highlight follows local view and hidden objects.
- Every row has buttons to select the objects or the problem parts, and a refresh icon that re-checks just that issue.
- Live mode re-checks objects a moment after you edit them.
- Issues with a safe automatic fix show a fix button when you open them: apply rotation and scale, triangulate n-gons, remove collapsed faces, merge by distance, recalculate flipped normals, delete loose geometry, rename to convention and more.

**Fix mode** applies a set of fixes in one go. Tick what you want, click Preview to see what would change, then Fix. The whole fix is one undo step. Objects never move in the world, and anything the add-on can't fix safely (shared meshes, linked data, mirrored scale unless you allow it) is skipped and listed.

## Bake Prep

**Name Pairs** names an unnamed set of lows and highs for you. Pick the collection with the lows, the collection with the highs and an asset name. Each high is matched to the low its surface sits on, so the old names don't have to match. Preview Names lists every rename first, then Rename Pairs does it as one undo step. Lows get `SM_<Asset>_<Part>_low` (meaningful old names are kept as the part, generic ones like `Cylinder.015` become Part01, Part02...), the main high gets `_high`, and extra pieces like screws get a tag from their old name. Matches that sit close to two lows are marked as worth a look.

**Find Pairs** reads the names and shows:

- each low with its highs (one low can have several highs)
- lows with no high, highs with no low, and meshes not named for baking, each with a select button
- per pair checks: the low has UVs, the low has no overlapping UVs (unless you mark them as stacked on purpose), the high covers the low, and rotation and scale match

Also in the panel:

- **Explode** spreads the pairs out along X, Y or Z so their bakes can't bleed into each other. Reset puts everything back exactly where it was.
- **Make Bake Groups** puts each pair in its own collection, named for its Marmoset bake group.
- **Export for Baking** writes a low and a high FBX per asset for Marmoset Toolbag or Substance Painter, with an option to triangulate the low. Extra highs are joined into one `_high` mesh in the bake file so name matching always finds them.

## Batch Export

Exports one FBX per asset into `<folder>/<Asset>/`, grouped by the `<Asset>` part of the name or by collection.

| Preset | Axes | Files |
| --- | --- | --- |
| Unreal | -Y forward, Z up | `SM_<Asset>.fbx` with `_low` dropped from mesh names, `UCX_` collision included, `_LOD1` objects in `SM_<Asset>_LOD1.fbx` |
| Substance | Y forward, Z up | `SM_<Asset>_low.fbx` and `SM_<Asset>_high.fbx`, triangulated |
| Marmoset | Y forward, Z up | `SM_<Asset>_low.fbx` and `SM_<Asset>_high.fbx`, triangulated |

- Every preset setting can be edited in the panel and is saved in the add-on preferences. Reset Preset puts the defaults back.
- Each asset is moved to the world origin (bottom center by default) for the export only.
- Dry Run lists every file with mesh and triangle counts, the objects it skipped, and any Scene Prep warnings for those objects. A real export also writes `om3d_export_log.txt`.
- Your scene is never changed. The export works on temporary copies with modifiers applied and deletes them afterwards.

## Reset

Each panel has a Reset link at the top right that clears its lists without touching the scene. Opening a file also starts all three panels empty, which is handy when you record a tutorial.

## Scripting

Every button is a regular Blender operator (`om3d.scene_prep_check`, `om3d.scene_prep_fix`, `om3d.bake_pair`, `om3d.bake_name_apply`, `om3d.export_batch` and so on), so the tools work from the UI, from Python scripts, or from an AI assistant over MCP.

## Known limitations

- Name Pairs matches by position. When two lows are nested inside each other a high can sit almost equally close to both. Those matches are flagged so you can check them.
- Overlapping UVs and overlapping faces are the slowest checks. Live mode skips them on meshes over 5000 faces and keeps the last result. Use the refresh icon to re-check.
- FBX is the only export format for now.

## About this tool

I built this for my own workflow and I'm sharing it as a starting point. Use it, change it, make it yours. It's provided as is, so test it on a copy of your scene first.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
