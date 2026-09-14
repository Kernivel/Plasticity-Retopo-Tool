# Development

The plugin was developed using Claude Code.
I am not claiming to be a Blender plugin expert, nor do I have experience with the mathematics and algorithms involved.
My input in this is guiding the user experience towards something comfortable and intuitive.

## Working from a checkout

```bash
git clone https://github.com/Kernivel/Plasticty-Retopo-Tool.git
cd Plasticty-Retopo-Tool
python scripts/deploy.py
```

`deploy.py` finds Blender's addons folder and copies the package into it, leaving
out the tests, the scripts and this documentation. Then enable **Plasticity
Retop** in `Preferences > Add-ons`.

To pick a specific Blender:

```bash
python scripts/deploy.py --list          # show the config dirs it found
python scripts/deploy.py --dest "<addons dir>"
```

**No system Python?** You can still use Blender's own interpreter to run it:

```bash
"C:/MyBlenderInstallFolder/Blender <version>/python/bin/python.exe" scripts/deploy.py
```

For instance:
```bash
"C:/Program Files/Blender Foundation/Blender 5.0/5.0/python/bin/python.exe" scripts/deploy.py
```

## Confirming it landed

Open the **Retop** tab 3D view's **N-panel**,*and look at the **version number**.

After deploying, use the panel's **Reload Addon**

**Both buttons, and the red stale-code warning, are behind Developer Mode** —
`Preferences > Add-ons > Plasticity Retop > Developer Mode`, off by default.

## Updating a checkout

```bash
git pull
python scripts/deploy.py
```

Then **Reload Addon Only**, and check the version string. Your settings survive a
reload — Blender stores them on the scene, keyed by name.

## Building a release zip

```bash
python scripts/build_zip.py          # dist/<name>-<version>.zip
python scripts/build_zip.py --check  # verify only, write nothing
```

The zip holds one top-level folder with the addon inside — the shape Blender's
installer expects — and excludes exactly what `deploy.py` excludes. It refuses to
build when `bl_info["version"]` and `version.py` disagree, because those are the
two numbers Blender's add-on list and the N-panel each show.

Pushing a `v<version>` tag runs `.github/workflows/release.yml`, which builds the
same zip and attaches it to the GitHub release.

## Commands

```bash
python scripts/run_tests.py     # headless test suite (needs Blender only)
```

Plasticity is **not** needed to develop or test: the tests build synthetic meshes
carrying the same custom properties the bridge writes.

Blender is found via `--blender`, `$BLENDER`, `PATH`, then the usual install
paths. There may be no system Python — both scripts are stdlib-only, so Blender's
bundled interpreter runs them.

## Testing & testing results
Testing covers the addon's precision and robustness when creating shapes.
Some basic shapes were created in Plasticity and exported to a .blend file.

[RESULTS.md](https://github.com/Kernivel/Plasticty-Retopo-Tool/blob/main/RESULTS.md) is the golden table of results.

`RESULTS.md` and the golden table in `tests/test_fixtures.py` are **generated —
never hand-edit either**:

```bash
blender tests/fixtures/TestCases.blend --background --factory-startup --python scripts/gen_results.py
blender tests/fixtures/TestCases.blend --background --factory-startup --python scripts/gen_expectations.py
```

A re-export of the fixture renumbers every Plasticity face id even when no vertex
moves, so a stale table makes "the fixture changed" indistinguishable from "the
code regressed". Regenerate and read the diff instead.

## The two rules that cost the most when broken

**Bump `version.py` on every change.** The panel shows it, and it is the only
reliable way to confirm a reload actually took. See
[Troubleshooting](../reference/troubleshooting.md#i-deployed-and-nothing-changed).

**`--factory-startup` is not optional** on any script run against
`tests/fixtures/TestCases.blend`, however read-only the script looks. Run without
it and every installed addon loads too — one of them once *saved the fixture over
itself* on quit (it leaves a `.blend1` beside it, which is the tell). The file is
frozen: `git status` after any run against it.


## Working on the docs

The site is MkDocs Material, in `docs/`, published to GitHub Pages by
`.github/workflows/docs.yml` on every push to `main` that touches it.

```bash
pip install -r requirements-docs.txt
mkdocs serve        # live reload on http://127.0.0.1:8000
mkdocs build --strict
```

`--strict` is what CI runs: a dead internal link fails the build.

Nothing in `docs/` ships with the addon — `scripts/deploy.py` skips it, along
with `mkdocs.yml`, `site/` and `requirements-docs.txt`.
