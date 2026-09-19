# Changelog

## [1.5.0](https://github.com/skjall/home-assistant-entity-manager/compare/v1.4.0...v1.5.0) (2026-09-19)


### Features

* the panel can be light, dark, or whatever the system says ([#145](https://github.com/skjall/home-assistant-entity-manager/issues/145)) ([34097c4](https://github.com/skjall/home-assistant-entity-manager/commit/34097c4dc745538523ce382ce956b3eb645d1e03))


### Bug Fixes

* a bracket that names the peer survives a rename of ours ([#144](https://github.com/skjall/home-assistant-entity-manager/issues/144)) ([39534d3](https://github.com/skjall/home-assistant-entity-manager/commit/39534d35cb5f359b33cb3ffe3ec390140d0c4621))
* a finished job shows its mark, not a gap ([#154](https://github.com/skjall/home-assistant-entity-manager/issues/154)) ([021f37b](https://github.com/skjall/home-assistant-entity-manager/commit/021f37bf324978b4053f11885ccb79940cc633d3))
* a message long enough to matter can still be closed ([#146](https://github.com/skjall/home-assistant-entity-manager/issues/146)) ([5732b2f](https://github.com/skjall/home-assistant-entity-manager/commit/5732b2ff3d41c555a05271da9fbafdc467a1c0a9))
* a number starts at one ([#150](https://github.com/skjall/home-assistant-entity-manager/issues/150)) ([8a87ce9](https://github.com/skjall/home-assistant-entity-manager/commit/8a87ce98612420fe8d1e27106b4fad0939e327c5))
* a row whose entity is gone does not argue about it ([#147](https://github.com/skjall/home-assistant-entity-manager/issues/147)) ([ce2bc1d](https://github.com/skjall/home-assistant-entity-manager/commit/ce2bc1d3cf5506606ac9d90d4d6a7b5c9d26dc1c))
* the job panel does not close under a reading eye ([#152](https://github.com/skjall/home-assistant-entity-manager/issues/152)) ([4426d7b](https://github.com/skjall/home-assistant-entity-manager/commit/4426d7b1b4153f7d2378fe7803e06f341e932427))
* the numbered row says it once, and says why ([#153](https://github.com/skjall/home-assistant-entity-manager/issues/153)) ([69f5934](https://github.com/skjall/home-assistant-entity-manager/commit/69f5934d5695a1105c415a3091ce4cd67bb83e10))
* two devices may share a name, two entities may not share an id ([#148](https://github.com/skjall/home-assistant-entity-manager/issues/148)) ([081347e](https://github.com/skjall/home-assistant-entity-manager/commit/081347ea8a981176172df38eb2130cd610ddcf46))

## [1.4.0](https://github.com/skjall/home-assistant-entity-manager/compare/v1.3.0...v1.4.0) (2026-09-19)


### Features

* a quiet way to keep the name an entity already has ([#127](https://github.com/skjall/home-assistant-entity-manager/issues/127)) ([0e1b6ca](https://github.com/skjall/home-assistant-entity-manager/commit/0e1b6ca12a9ce12299df94623333578988226ddf))
* a rule can say which domain it means ([#128](https://github.com/skjall/home-assistant-entity-manager/issues/128)) ([03aa1f3](https://github.com/skjall/home-assistant-entity-manager/commit/03aa1f3430b6e9395fe8e8856886dd23b05aa3bb))


### Bug Fixes

* a change writes what moved, instead of building the list again ([#136](https://github.com/skjall/home-assistant-entity-manager/issues/136)) ([d260cc4](https://github.com/skjall/home-assistant-entity-manager/commit/d260cc4cb5d7f4a9b55391ec3c57e0ea383226cc))
* a name a template finishes is not an entity id ([#119](https://github.com/skjall/home-assistant-entity-manager/issues/119)) ([2575c04](https://github.com/skjall/home-assistant-entity-manager/commit/2575c04a55ddb9bdea435347b1fa481167ab15d6))
* a reload on a phone stays where the reader was ([#122](https://github.com/skjall/home-assistant-entity-manager/issues/122)) ([bf604a9](https://github.com/skjall/home-assistant-entity-manager/commit/bf604a940c97fe24a9c8182129a53cbc75f58920))
* a switched-off device has nothing to change ([#139](https://github.com/skjall/home-assistant-entity-manager/issues/139)) ([2675aff](https://github.com/skjall/home-assistant-entity-manager/commit/2675affa64f44d57623893d5b62a598cf691db75))
* a switched-off device offers the one thing that works on it ([#131](https://github.com/skjall/home-assistant-entity-manager/issues/131)) ([33f563f](https://github.com/skjall/home-assistant-entity-manager/commit/33f563fa49d722202678d809a8172e0374a48cd0))
* a word an integration supplies belongs to the integration ([#137](https://github.com/skjall/home-assistant-entity-manager/issues/137)) ([0031c23](https://github.com/skjall/home-assistant-entity-manager/commit/0031c233fa0c36cba9280240221611c075f38040))
* an unsaved edit keeps its card under "only changes" ([#126](https://github.com/skjall/home-assistant-entity-manager/issues/126)) ([b31ba65](https://github.com/skjall/home-assistant-entity-manager/commit/b31ba652a5f22db55814607ca272970b03ffbd58))
* applying everything keeps the rules that were typed ([#130](https://github.com/skjall/home-assistant-entity-manager/issues/130)) ([3844190](https://github.com/skjall/home-assistant-entity-manager/commit/38441903bab24ab2e02fef988850141935280840))
* let a rule be reworded where it already speaks ([#134](https://github.com/skjall/home-assistant-entity-manager/issues/134)) ([0d63d6b](https://github.com/skjall/home-assistant-entity-manager/commit/0d63d6b903426f7fe473e0ff0a3db1e2a2f55fb2))
* let the type field fill the card on a phone ([#120](https://github.com/skjall/home-assistant-entity-manager/issues/120)) ([df19df3](https://github.com/skjall/home-assistant-entity-manager/commit/df19df334cc3b6b2c079a3420a09f267311e9f96))
* link to Home Assistant by path, not by a port we picked ([#124](https://github.com/skjall/home-assistant-entity-manager/issues/124)) ([bedb3f0](https://github.com/skjall/home-assistant-entity-manager/commit/bedb3f04ec33a6d016590809fca76f8eaa7291cc))
* one button, and it keeps what was typed ([#125](https://github.com/skjall/home-assistant-entity-manager/issues/125)) ([4fccd84](https://github.com/skjall/home-assistant-entity-manager/commit/4fccd8450b8e6bf5b0a4240165682c86713ea466))
* one change does not reload the list in front of the reader ([#129](https://github.com/skjall/home-assistant-entity-manager/issues/129)) ([10445af](https://github.com/skjall/home-assistant-entity-manager/commit/10445af8123fcd207bd76721680323098e148a55))
* one tick anywhere means the button for all of them is there too ([#138](https://github.com/skjall/home-assistant-entity-manager/issues/138)) ([8ea4db6](https://github.com/skjall/home-assistant-entity-manager/commit/8ea4db62d1bde89aac4211a3030e3e72891bc38d))
* open Home Assistant's own pages in the app, not a browser ([#123](https://github.com/skjall/home-assistant-entity-manager/issues/123)) ([c23b607](https://github.com/skjall/home-assistant-entity-manager/commit/c23b60701eb22fb4c0fe8c70a3e02742dfaa09c0))
* say where a helper's name comes from once ([#121](https://github.com/skjall/home-assistant-entity-manager/issues/121)) ([34427eb](https://github.com/skjall/home-assistant-entity-manager/commit/34427eba68853b7a41364b0d93a4dc6cec80b107))
* take a name apart the way it was put together ([#133](https://github.com/skjall/home-assistant-entity-manager/issues/133)) ([b58c95e](https://github.com/skjall/home-assistant-entity-manager/commit/b58c95e2406f40e78f4a89043709be2e05b06e48))
* wait out a Home Assistant restart instead of reporting it ([#135](https://github.com/skjall/home-assistant-entity-manager/issues/135)) ([9aa5af6](https://github.com/skjall/home-assistant-entity-manager/commit/9aa5af60e4db2fc3b1a39543425860c73435d55c))

## [1.3.0](https://github.com/skjall/home-assistant-entity-manager/compare/v1.2.0...v1.3.0) (2026-09-17)


### Features

* add configurable naming templates ([#95](https://github.com/skjall/home-assistant-entity-manager/issues/95)) ([2246839](https://github.com/skjall/home-assistant-entity-manager/commit/224683911e6c34731d1ba7a5e43907b8987d55df))
* carry renames into the helpers built in the interface ([#113](https://github.com/skjall/home-assistant-entity-manager/issues/113)) ([fe5691e](https://github.com/skjall/home-assistant-entity-manager/commit/fe5691e12b0c3af200997188956597db7ac5d043))
* name entities from rules and templates, with provenance, an API and an MCP server ([#109](https://github.com/skjall/home-assistant-entity-manager/issues/109)) ([2cb79f7](https://github.com/skjall/home-assistant-entity-manager/commit/2cb79f74646191c312f5b6321f83f26758e9dbbc))
* name the references a rename cannot carry, and answer from the rename log ([#118](https://github.com/skjall/home-assistant-entity-manager/issues/118)) ([e6534cc](https://github.com/skjall/home-assistant-entity-manager/commit/e6534cce040d3b70b982ffcb835afa7d919885ae))
* rules that say where they apply, and renames that are read back ([#116](https://github.com/skjall/home-assistant-entity-manager/issues/116)) ([f7ad6e7](https://github.com/skjall/home-assistant-entity-manager/commit/f7ad6e71c1227daff7a49ecba73da2d4dc8ec397))


### Bug Fixes

* apply configured type mappings to entity names ([#105](https://github.com/skjall/home-assistant-entity-manager/issues/105)) ([1597fcf](https://github.com/skjall/home-assistant-entity-manager/commit/1597fcf2e664fce92db03b129fcaec80b044e9a9))
* ask what we noted before what the integration froze ([#112](https://github.com/skjall/home-assistant-entity-manager/issues/112)) ([0004159](https://github.com/skjall/home-assistant-entity-manager/commit/0004159ae1d7a59f5fb6f846f3d1d20e7340b4c5))
* call the Zigbee2MQTT helper again, not the route ([#111](https://github.com/skjall/home-assistant-entity-manager/issues/111)) ([d8d603d](https://github.com/skjall/home-assistant-entity-manager/commit/d8d603ddb813ca1f20a092e454305fe54efa4d8d))
* carry the area over and remove the old device completely when swapping ([#108](https://github.com/skjall/home-assistant-entity-manager/issues/108)) ([d80ffc3](https://github.com/skjall/home-assistant-entity-manager/commit/d80ffc3f22c580880be119750be7fe8893e39c16))
* give every proposed entity ID a unique target ([#106](https://github.com/skjall/home-assistant-entity-manager/issues/106)) ([fc2f89d](https://github.com/skjall/home-assistant-entity-manager/commit/fc2f89d2be8cab1b30b027a780c052227e2220a3))
* keep job failures and modal content on screen ([#102](https://github.com/skjall/home-assistant-entity-manager/issues/102)) ([32f92bc](https://github.com/skjall/home-assistant-entity-manager/commit/32f92bccf5753ac538699de6ba8489f9daa66f93))
* skip Claude review jobs on pull requests from forks ([#98](https://github.com/skjall/home-assistant-entity-manager/issues/98)) ([65bef5b](https://github.com/skjall/home-assistant-entity-manager/commit/65bef5b8c187e9bbe8d67d227d140f306dabaadb))
* stop centring the device list on a phone ([#114](https://github.com/skjall/home-assistant-entity-manager/issues/114)) ([b04835c](https://github.com/skjall/home-assistant-entity-manager/commit/b04835c7d04c7c05b5cf180c5de5a65af801a87f))

## [1.2.0](https://github.com/skjall/home-assistant-entity-manager/compare/v1.1.0...v1.2.0) (2026-07-02)


### Features

* track long-running operations as async background jobs ([#84](https://github.com/skjall/home-assistant-entity-manager/issues/84)) ([19efa09](https://github.com/skjall/home-assistant-entity-manager/commit/19efa09c47770a4200053bdcd3c803ae690f25d8))
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
