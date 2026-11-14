# Changelog

## 0.7.1
- Hard edges, no UV seam now sees hard edges made by Shade Auto Smooth (the Smooth by Angle modifier). Before, those meshes passed the check with 0 hard edges.
- Overlapping UVs no longer freezes on stacked UVs. A 10,000 face mesh with every face stacked took about 19 seconds; it now takes a fraction of a second.
- Mesh cleanup (merge, delete loose, collapsed faces, triangulate, recalculate normals) and Remove Empty Slots now skip meshes shared by several objects, and list them, as the Fix description always said. Library overrides are treated like linked data.
- Explode and Reset: every object goes back exactly where it was, including a part parented to an object from another pair.
- Rename to Convention and Name Pairs keep an existing prefix and asset name out of the part name: `SM_Crate_Lid` becomes `SM_Crate_Lid_low`, not `SM_Crate_SMCrateLid_low`.
- Apply Rotation and Scale skips objects with a Multires modifier and lists them (use Object > Apply for those, which rescales the sculpt detail).
- Remove Group Empties leaves an empty alone when some of its children are outside the scope, or when a modifier or constraint uses it (a mirror object, for example).
- Batch Export hands its copies straight to the FBX exporter, so exporting from Local View can't write an empty file. Sculpt, Weight Paint and other modes are restored after export, and collection names are made safe to use as folder names.
- Remove collapsed faces uses the same threshold as the Collapsed faces check.
- Live update keeps working after a file is opened while a refresh was pending.
- Name Pairs results no longer break on object names that contain `|`.

## 0.7.0
- New check: Hard edges, no UV seam. Finds edges where the shading splits (sharp marks, flat faces, smooth by angle or imported custom normals) but the UVs don't, since every hard edge needs a UV seam for a clean normal map bake.

## 0.6.5
- Overlapping faces only counts faces that face the same way (z-fighting) or a face folded back over its neighbour. Separate parts touching back to back, and overlaps under 1% of a face, no longer count.

## 0.6.4
- Flipped normals confirms an inside-out shell with a ray test, so shells that fold through themselves aren't flagged by mistake.

## 0.6.3
- Scene Prep Reset link. Opening a file starts Scene Prep, Bake Prep and Batch Export with empty lists and clears the viewport highlight.

## 0.6.2
- Bake Prep Reset link and a Clear link for Batch Export results.

## 0.6.1
- Name Pairs shows close matches as a note to check instead of an error.

## 0.6.0
- Name Pairs: match highs to lows by position and rename both to the convention, with a preview and one undo step.

## 0.5.1
- Overlapping UVs fills its faces only while its row is open.

## 0.5.0
- Bake Prep: pairing by name with several highs per low, unmatched lists, per pair checks, Explode and Reset, bake group collections, export for Marmoset and Substance.
- Batch Export: one FBX per asset, Unreal, Substance and Marmoset presets, UCX collision and LOD files, dry run and export log.

## 0.4.x
- Overlapping UVs fills its faces in the highlight.
- Opening an issue shows only its highlight. The highlight follows local view and hidden objects.
- Overlapping verts tolerance scales with the mesh size. Overlapping UVs are counted per material.
- Refresh icon on every issue and a fix button inside issues that have a safe fix. Triangulate n-gons and Remove collapsed faces fixes.

## 0.3.x
- Fix mode with Preview, one undo step, and a grouped log of what changed and what was skipped.

## 0.2.x
- Checker set modeled on xView, viewport highlight, live refresh, tooltips, legend, per check toggles.

## 0.1.0
- Scene Prep check only.
