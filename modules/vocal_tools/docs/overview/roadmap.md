# Roadmap

The current direction is feature expansion first, followed by broader integration testing.

## Catalog and Model Management

- Add official public manifest URLs for SHS AI Tools.
- Add model download policy and checksums for public MDX models.
- Add remote RVC voice model manifest download/install support.
- Add model license and safety metadata where available.
- Add UI grouping and search metadata for large model lists.
- Add optional manifest signature/trust policy if public catalogs grow.

## Library Packaging and API Cleanup

- Keep the current top-level public API stable while downstream projects adopt the library.
- Keep `vocal_tools/rvc_catalog.py` as a compatibility shim for now; prefer `vocal_tools.catalog.voice_catalog` internally.
- Audit base dependencies before moving shared runtime packages into smaller extras. The current base install is still heavy because it includes packages such as `torch`, `librosa`, and `scipy` for backward compatibility.
- Design a future minimal base extra split, for example keeping catalog/task/CLI metadata light while moving MDX, RVC, and audio processing runtimes into extras.
- Consider moving legacy MDX direct API docs under a compatibility section once all examples use `ModelCatalog + Separator`.
- Split examples into `examples/basic`, `examples/verify`, and `examples/templates` only if the flat folder becomes hard to scan. Avoid a large rename until downstream references are known.
- Expand lightweight automated checks only when they stay inference-free and standard-library friendly.

## Engines

- Select and integrate a concrete bundled VR Arch backend.
- Select and integrate a concrete bundled MDXC/Roformer backend.
- Add real integration tests for VR Arch and MDXC after backend selection.
- Improve MDX option coverage for UVR-like workflows.
- Improve Demucs runtime selection and progress parsing.

## Pipeline

- Tune optional dereverb quality.
- Add clearer main/backing vocal split policy.
- Add optional post-processing stages.
- Add richer result metadata for every intermediate artifact.
- Add safe cleanup behavior for generated intermediates.

## SHS Integration

- Keep the library headless.
- Do not add SHS GPU Node or SHS AI Tools connections inside this package.
- Let SHS GPU Node own queue, cancellation, and progress transport.
- Let SHS AI Tools own model browsing, download UX, and job creation.
- Use task payload helpers as the stable boundary between host services and the engine.
- Expand model listing payloads as SHS AI Tools needs filtering, paging, or search.

## Testing Later

- Add integration tests after feature surfaces settle.
- Avoid repeating long real inference tests for every small change.
- Keep lightweight import, AST, and manifest checks as quick smoke tests.
- Keep real MDX, Demucs, RVC, and cover pipeline checks as explicit release/integration tasks rather than default unit tests.
