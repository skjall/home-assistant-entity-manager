# Changelog

## [1.2.0](https://github.com/skjall/home-assistant-entity-manager/compare/v1.1.0...v1.2.0) (2026-07-01)


### Features

* use slugify for full non-ASCII entity name normalization ([#78](https://github.com/skjall/home-assistant-entity-manager/issues/78)) ([ca3c802](https://github.com/skjall/home-assistant-entity-manager/commit/ca3c8026b6fcdfa57c7608d8f5e093e2237dda8d)), closes [#58](https://github.com/skjall/home-assistant-entity-manager/issues/58)

## [1.1.0](https://github.com/skjall/home-assistant-entity-manager/compare/v1.0.3...v1.1.0) (2026-06-26)


### Features

* add searchable audit log of entity renames ([#66](https://github.com/skjall/home-assistant-entity-manager/issues/66)) ([048da82](https://github.com/skjall/home-assistant-entity-manager/commit/048da82d6bde441995e0f40d24297a9359d14659))
* add YAML path tracking to broken reference detection ([86d5db9](https://github.com/skjall/home-assistant-entity-manager/commit/86d5db9639ada175eb2f1514220b4444f2019e43))
* device-swap core (job store, mapping, state machine) ([8842bc4](https://github.com/skjall/home-assistant-entity-manager/commit/8842bc4d1f6b8178329750121a1f90e8b09f353b))
* device-swap REST API endpoints ([3e02a9e](https://github.com/skjall/home-assistant-entity-manager/commit/3e02a9e2dc9e54cbc0b164a30a02bcddfc32e53d))
* device-swap wizard UI (4-step stepper) ([d9ab061](https://github.com/skjall/home-assistant-entity-manager/commit/d9ab06189537c0cff77c9afa39efae299df2ae41))
* entity-ref utils + integration bridge foundation ([1f98a20](https://github.com/skjall/home-assistant-entity-manager/commit/1f98a200f779cedc136f16a5da128ed07d7c9412))
* Migrate to Tailwind CSS v4 ([9095a6b](https://github.com/skjall/home-assistant-entity-manager/commit/9095a6b98e05ee4db5fd4cce6cc8181f9394de25))
* **rename:** Teil C - Z2M friendly_name homogenization on normal rename ([f238c8f](https://github.com/skjall/home-assistant-entity-manager/commit/f238c8ffc21909c29314ec4d197e1d8c2e504ec2))
* **swap:** carry over friendly names (prefix swap) ([77d528c](https://github.com/skjall/home-assistant-entity-manager/commit/77d528ca0bc74e2cc44bfdd33a3f693ffb0f9992))
* **swap:** Etappe 5 - MQTT/Z2M bridge (native rename + remove) ([5e5035c](https://github.com/skjall/home-assistant-entity-manager/commit/5e5035c97c9e096654779060f117fa3fcf8adfb8))
* **swap:** log a RESUME entry on retry/resume ([9531997](https://github.com/skjall/home-assistant-entity-manager/commit/95319976c5bac6656ae1ab87cca9f83e4df591a2))
* **swap:** Lovelace dashboard support (Etappe 4) ([e4d6393](https://github.com/skjall/home-assistant-entity-manager/commit/e4d639378a5ebdfa302d9fd525eec06fd77239da))
* **swap:** map only in-use entities; resume to correct step ([c319354](https://github.com/skjall/home-assistant-entity-manager/commit/c31935437268370a711c1d0af13e87d9d8d55e0f))
* **swap:** new device takes over old device's identity (all entities) ([cc46536](https://github.com/skjall/home-assistant-entity-manager/commit/cc465369a86611ff469a623dbe4f3987681cd295))
* **swap:** redesign wizard modal + robust inline combobox ([099c28a](https://github.com/skjall/home-assistant-entity-manager/commit/099c28a1e204a2d908cba4923992a9ee266846b7))
* **swap:** rewire device triggers (device_id) too ([eabb747](https://github.com/skjall/home-assistant-entity-manager/commit/eabb747d6a5be8e53d52ef4710146c2062553c70))
* **swap:** suffix-based mapping by entity name; drop confidence badge ([ab5d605](https://github.com/skjall/home-assistant-entity-manager/commit/ab5d605232ca5e26a014c0cb48ffd11585de5573))
* **swap:** YAML dashboard scan + read YAML for in-use ([f3f7b77](https://github.com/skjall/home-assistant-entity-manager/commit/f3f7b77d8d64f08b7a51f4021e2a547c61d27b56))
* **z2m:** show Z2M name drift in device view + sync button + apply-all ([e372220](https://github.com/skjall/home-assistant-entity-manager/commit/e3722202992925c3dff3bdc8508a19d608de6480))


### Bug Fixes

* add missing modules to Dockerfile, bump to 1.0.0-beta ([e4e6d7b](https://github.com/skjall/home-assistant-entity-manager/commit/e4e6d7b666d23a40a71fcab3ffc37b588add0b47))
* Checkout correct branch for PR context in Crowdin sync ([8e14cce](https://github.com/skjall/home-assistant-entity-manager/commit/8e14cce8da06ea37cecebba954b4b0ecc6633012))
* CI always runs on PRs to main (no paths-ignore) ([7af08b2](https://github.com/skjall/home-assistant-entity-manager/commit/7af08b22d3802850d85734ff10f27894d093b270))
* Crowdin pushes directly instead of creating PR ([4d37030](https://github.com/skjall/home-assistant-entity-manager/commit/4d370306c75158c0504bb99ca98b310bbba0b332))
* **deps:** update dependency remixicon to v4 ([587431b](https://github.com/skjall/home-assistant-entity-manager/commit/587431b08cc3fb7ed9ee91fa3023e9773e14a611))
* **deps:** update dependency remixicon to v4 ([f289de3](https://github.com/skjall/home-assistant-entity-manager/commit/f289de3cf10df53559e2598afbbbe07ab85d33da))
* Remove skip ci from Crowdin commits ([7a118d4](https://github.com/skjall/home-assistant-entity-manager/commit/7a118d446f29948abc51043483086dee8ebc7634))
* Remove tailwind.config.js from Dockerfile (Tailwind v4 migration) ([7371dbe](https://github.com/skjall/home-assistant-entity-manager/commit/7371dbed9b360b919721590a679e6c48da78f5ea))
* Run Crowdin sync on PRs to main ([9824e9c](https://github.com/skjall/home-assistant-entity-manager/commit/9824e9c35813f5baf9977b170a21a5f108e821e5))
* Skip Claude review for bot PRs (Renovate, etc.) ([a569893](https://github.com/skjall/home-assistant-entity-manager/commit/a569893b0a9b6cd76db7b824b78975e9c4304154))
* Skip Claude review for bot PRs (Renovate, etc.) ([5d0274b](https://github.com/skjall/home-assistant-entity-manager/commit/5d0274b392c93268fdfbe4f54fd273084db272a0))
* **swap:** abort deletes job (PROPOSED/CONFIRMED) instead of marking ABORTED ([4cc6286](https://github.com/skjall/home-assistant-entity-manager/commit/4cc6286fb353a109a0083b647f458dc47af85fa3))
* **swap:** combobox dropdown closing instantly + z-index overlap ([b499513](https://github.com/skjall/home-assistant-entity-manager/commit/b499513a5ad9b62a643f52e19e16a24c841bb0e6))
* **swap:** distinct icon + searchable device pickers ([dbf9a93](https://github.com/skjall/home-assistant-entity-manager/commit/dbf9a9373c961de6d842a622e367f1228cc6ad61))
* **swap:** exclude the other selected device (no self-swap) ([1b45b1f](https://github.com/skjall/home-assistant-entity-manager/commit/1b45b1f519b8cb623a7687a3c417bec6a596d035))
* **swap:** mapping selects show proposal + type-filtered options ([a94872d](https://github.com/skjall/home-assistant-entity-manager/commit/a94872dc0e2378f50b6e65f498f1fad5e4d40d68))
* **swap:** native remove uses correct device_id key; partial success ([30f9a24](https://github.com/skjall/home-assistant-entity-manager/commit/30f9a241ea5fbbeae631314e8f57952298e5c279))
* **swap:** RENAMING_ENTITIES uses prefix swap, not generate_new_entity_id ([a021ae5](https://github.com/skjall/home-assistant-entity-manager/commit/a021ae563679b48460ec024ab7e5c10d48c887d2))
* **swap:** repair modal layout (flat-strip bug) + full-width inputs ([bd21d53](https://github.com/skjall/home-assistant-entity-manager/commit/bd21d534eeb403146eacab33933f15a619fb991f))
* **swap:** resume jumps to correct step by job state ([0be6b0a](https://github.com/skjall/home-assistant-entity-manager/commit/0be6b0a7d7638cb7925f2079ab2877d08305405d))
* **swap:** robust device combobox (reopen, no stale text, normalized search) ([35bf1da](https://github.com/skjall/home-assistant-entity-manager/commit/35bf1da7f50b580f692843b7b8cf99a8254c3e63))
* **swap:** search icon padding (area-select overrode pl-9 via [@layer](https://github.com/layer)) ([18c1994](https://github.com/skjall/home-assistant-entity-manager/commit/18c19947d831137af5f672caed76c2474ace3dcd))
* updated renovate config. ([585f6ae](https://github.com/skjall/home-assistant-entity-manager/commit/585f6ae0893816258ce0c6ad661fc505481d4b2d))
