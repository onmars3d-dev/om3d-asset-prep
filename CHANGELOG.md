# Changelog

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
