# ผลการทดลอง Phase 0

ทดสอบเมื่อ 2026-10-17 กับเกมจริงบนเครื่อง dev

## สรุป

| # | สมมติฐาน | ผล |
|---|---|---|
| H1 | UI layout เก็บเป็นไฟล์ข้อมูลที่แก้ได้ | **น่าจะไม่จริง** ไฟล์ `.RT` มีแค่ ID → path ของ bitmap ไม่มีพิกัด layout น่าจะฝังใน exe |
| H2 | UI bitmap อยู่ในไฟล์ที่แกะ/แทนที่ได้ | **จริง** อยู่ใน `UIGraphics\UIGraphics.far` เป็น BMP แกะได้ด้วย `tools/far.py` |
| H3 | เกมอ่านไฟล์ที่แก้แล้ว | **จริง** วางไฟล์ loose ที่ `UIGraphics\<โฟลเดอร์>\<ชื่อ>` แล้วเกมใช้แทนไฟล์ใน FAR ไม่มี checksum |
| H4 | บังคับให้วาด UI 1:1 ที่ 4K ได้ | **จริง ไม่ต้องแพตช์ exe** ใช้ launch option `-dpi:1` + ตั้ง Resolution ในเกมเป็น `3840x2160 (16:9)` |

**ทางลัดสำหรับผู้เล่นทั่วไป (ไม่ต้อง mod):** ตั้ง Resolution เป็น `3840x2160 (16:9)` แล้วใส่ `-dpi:2` → เกมทำงานที่ 1920×1080 แล้วขยาย 2× แบบ nearest-neighbor UI คม ในบ้านเป็นจอกว้าง ไม่ต้องเปลี่ยนความละเอียดเดสก์ท็อป จึงน่าจะใช้แทน `launcher/` ได้

## สภาพแวดล้อมที่ทดสอบ

- Steam app 3314060, buildid 17905164, `changelist.txt` = 936127, `eadpsdk.json` applicationVersion 1.10.0
- `Sims.exe` 7,125,288 ไบต์
- จอหลัก 3840×2160 @160Hz, Windows scaling 200%, RTX 5090 (มีจออื่นอีก 2 จอ)

## ข้อเท็จจริงที่พบ

### Sims.exe

- PE32 (32-bit), ImageBase 0x400000, เปิด ASLR (`DllCharacteristics` 0x8140) ตอนรันจริงได้ base 0x220000 (เปลี่ยนทุกครั้ง)
- **ห่อด้วย SteamStub DRM** (มี section `.bind`) ส่วน `.text` ในไฟล์ถูกเข้ารหัส จึงวิเคราะห์โค้ดจากไฟล์ตรงๆ ไม่ได้ ต้อง dump หน่วยความจำของโปรเซสที่กำลังรัน (ดู "วิธีทำซ้ำ")
- string ใน `.rdata` อ่านได้ปกติ
- renderer ใหม่เป็น **Vulkan** (`vulkan-1.dll`, Vulkan Memory Allocator, namespace `tsvr`) ยังมี string error ของ DirectDraw/D3D เดิมค้างอยู่
- มี `SetProcessDPIAware`

### ที่เก็บข้อมูลผู้เล่นและการตั้งค่า

- ข้อมูลจริงอยู่ที่ `%USERPROFILE%\Saved Games\Electronic Arts\The Sims 25\UserData\` **ไม่ใช่** โฟลเดอร์ติดตั้ง (`UserData*` ในโฟลเดอร์ติดตั้งเป็นแค่ template)
- `Options.iff` อยู่ในโฟลเดอร์นั้น เกมเขียนตอนออกจากเกม (และเขียนไฟล์ `Characters\User0000x.iff` ของครอบครัวที่เพิ่งเข้าไปด้วย)
- Registry `HKCU\SOFTWARE\Electronic Arts\The Sims 25` มีแค่ภาษา, SKU, edition และ neighborhood ปัจจุบัน ไม่มีค่าการแสดงผล

### ตัวเลือก command line

- รูปแบบ `-ชื่อ:ค่า` หรือ `-ชื่อ_ค่า` (ตัวแยกคือ `:` หรือ `_` ตัวแรก)
- เกมเปิด whitelist ไว้ (flag ที่ RVA 0x67F48E = 1) อ่านได้เฉพาะ 26 ชื่อนี้ (ตาราง pointer ที่ RVA 0x651EF8):
  `dpi`, `monitor`, `maxaspect`, `w`, `skip_intro`, `crashtest`, `lUKEnglish` … `lUSEnglish` (ภาษา 19 ตัว), `skipLocaleValidation`
- `-r1024x768` แบบเดิมไม่อยู่ใน whitelist จึงถูกเมิน (ตรงกับที่ชุมชนรายงาน)
- ทดสอบโดยรัน `Sims.exe -dpi:N` ตรงๆ: SteamStub สั่ง Steam เปิดเกมใหม่และอาร์กิวเมนต์ยังอยู่ ส่วนการใส่ใน Steam Launch Options ยังไม่ได้ลองตรงๆ แต่ควรได้ผลเหมือนกัน
- `monitor`, `maxaspect`, `w`, `skip_intro` ยังไม่ได้ทดสอบ

### ตัวเลือกกราฟิกในเกม (Options → ไอคอนจอ)

ภาค 1 Legacy build นี้ **มี** ตัวเลือกความละเอียดในเกม (ข้อมูลเดิมใน CLAUDE.md ที่ว่าไม่มีนั้นล้าสมัยแล้ว):

- **Monitor:** `Auto` หรือเลือกจอ
- **Resolution:** บนจอ 4K มีให้เลือก `800x600 (4:3)`, `1066x600 (16:9)`, `1024x768 (4:3)`, `1364x768 (16:9)`, `1440x1080 (4:3)`, `1920x1080 (16:9)`, `2880x2160 (4:3)`, `3840x2160 (16:9)`
- **Windowed:** checkbox
- มีผลหลังเปิดเกมใหม่
- ค่าที่เจอตอนเริ่มคือ Monitor `Auto`, Resolution `1024x768 (4:3)`, Windowed ปิด (ไม่รู้ว่าเป็นค่า default หรือผู้ใช้ตั้งไว้) หลังทดสอบได้ตั้งกลับเป็นค่าเดิมแล้ว
- `Options.iff`: chunk `Optn` (id 128) เวอร์ชัน 8 เก็บค่าเหล่านี้เป็นสตริงลงท้ายด้วย NUL ต่อท้ายข้อมูลเดิม เช่น `Auto`, `Auto`, `3840x2160 (16:9)` (ยังไม่ได้ถอด layout ทุก field)

### สูตรคำนวณสเกล

ถอดจากฟังก์ชันตั้งค่าหน้าจอ (อ้างอิง string ที่ RVA 0x2F2609 `monitor`, 0x2F2681 `maxaspect`, 0x2F2BC0 `dpi`; ตัวอ่านอาร์กิวเมนต์อยู่ที่ RVA 0x2E1550):

1. `W, H` = ขนาดจาก Resolution ที่เลือก (ถ้าไม่ตรงรายการ ใช้ขนาดจอ)
2. สเกล default `s0 = H / 600` (หารปัดเศษทิ้ง) → 2160p ได้ 3, 1080p ได้ 1
3. ถ้ามี `-dpi:N` → `s = clamp(N, 1, s0)` ไม่เช่นนั้น `s = s0` (ชื่อ `dpi` ชวนเข้าใจผิด จริงๆ คือตัวหารสเกลแบบจำนวนเต็ม)
4. `-maxaspect:W:H` แต่ละค่า 1–999 ถูก clamp ไว้ที่ [4/3, 25] ค่า default 25 (แทบไม่จำกัด)
5. `-monitor:N` เลือกจอ N (1–25)
6. ความละเอียดตรรกะ = `min(W, round(H × maxaspect)) / s` × `H / s`

### ผลการทดลองบนจอ 4K

| Resolution ในเกม | `-dpi` | ความละเอียดตรรกะ | ผล |
|---|---|---|---|
| `1024x768 (4:3)` | ไม่ใส่ หรือ 1 | 1024×768 | ในบ้านเป็น 4:3 มีขอบดำ ขยาย 2.8125× แบบนุ่ม (เบลอ) |
| `3840x2160 (16:9)` | ไม่ใส่ | 1280×720 | ขยาย 3× UI ใหญ่มาก (อาการ "UI ใหญ่เกิน" ที่ชุมชนบ่น) |
| `3840x2160 (16:9)` | 2 | 1920×1080 | ขยาย 2× UI คมแบบ nearest-neighbor |
| `3840x2160 (16:9)` | 1 | 3840×2160 | **UI 1:1 คม แต่เล็กมาก** ตัวบ้านในเกมก็ถูกวาด 1:1 ด้วย (เล็กลงมาก) |

- หน้า neighborhood เป็นภาพ 1024×768 คงที่ วางกลางจอและคูณด้วย `s` ที่ `-dpi:1` ได้ 1024×768 พิกเซลจริงพอดี (x 1408–2432, y 696–1464)
- ภาพ splash ตอนโหลดยืดเต็มความสูงเสมอ

### การเรนเดอร์: UI กับโลกในบ้านแยกกัน

วัดจาก screenshot ที่ `-dpi:2` สองแบบ:

- **บล็อก 2×2 สีเดียวกันไหม:** control panel และพื้นหลังเทาไม่สม่ำเสมอ 0% หญ้าและตัวบ้านไม่สม่ำเสมอ 88–94% (การทดสอบนี้แยก "วาดที่ 4K" กับ "ขยาย bilinear 2×" ไม่ได้)
- **พลังงานที่ความถี่ Nyquist** (ผลรวมสลับเครื่องหมายของ 8 พิกเซล หารด้วยค่าต่างเฉลี่ย): การขยาย 2× ทุกแบบให้ค่าเกือบ 0 ส่วนภาพที่วาดที่ความละเอียดจริงไม่เป็น 0
  - control panel: 0.00 → ขยายจากความละเอียดตรรกะแบบ nearest
  - หญ้า, ผนัง, ประตู, ตัว Sim: 0.23–0.29 เท่ากับภาพ `-dpi:1` ที่เป็น native (0.25–0.28) → **โลกในบ้านถูกวาดที่ 4K จริง** (sprite ถูกขยายเป็น quad แล้ววาดลง target ขนาดจอ ตัว Sim 3D ก็ rasterize ที่ 4K)

### Pipeline การวาดจาก RenderDoc (2026-10-17)

จับเฟรมในบ้านที่ Resolution `3840x2160 (16:9)` + `-dpi:2` (RenderDoc 1.46 จับ Vulkan 32-bit ได้ ต้องวาง `steam_appid.txt` ไว้ข้าง exe เพื่อไม่ให้ Steam เปิดเกมซ้ำ) แต่ละเฟรมมี 5 ขั้น:

1. **โลกในบ้าน** วาดตรงลง target 3840×2160 (~1,000 draw) แต่ละ draw เป็น quad ของ texture เล็กๆ (พื้น 63×32, sprite เป็นคู่ texture สี+ความลึก) ส่วนพื้นผิวดินเป็น mesh วาดที่ความละเอียดเต็ม
2. **control panel** วาดลง target ของตัวเอง **ขนาด 1× (220×183)** (24 draw, sampler Point) จาก bitmap แยก texture เช่น sprite sheet 1018×43, 1018×52 และปุ่มต่างๆ
3. **หน้าต่าง/กล่องข้อความ** (เช่น Tutorial) วาดลง target ขนาด 1× ของตัวเอง (367×185) **ตัวหนังสืออยู่ในเลเยอร์นี้ด้วย**
4. **ประกอบภาพ** ลง target 3840×2160 ใหม่: draw ละเลเยอร์ (โลก 1 draw + UI ละ 1 draw) โดยขยายเลเยอร์ UI ×2 ด้วย **sampler Point (nearest)**
5. copy ลง swapchain 1:1 (Linear)

ที่ Resolution `1024x768 (4:3)` เกมวาดทุกอย่างลง target 3072×2304 (= 1024×768 × 3) แล้วย่อลงจอแบบ Linear จึงเบลอ

**ผลต่อทางเลือก:** UI เป็นกรณีเลเยอร์แยก ทาง ข ทำได้ตรงที่สุดคือเปลี่ยน draw ขั้น 4 ที่อ่านเลเยอร์ UI จาก Point เป็น shader xBR ส่วนทาง ก ต้องขยาย target ของเลเยอร์ UI เป็น 2× (พร้อม viewport) แล้วจึงแทน texture ด้วยรูป 2× ได้

### ไฟล์ UI

- `UIGraphics\UIGraphics.far` 794 ไฟล์: `cpanel` 391 BMP, `Nbhd` 64, `NghUI` 37, `Fonts` 45 `.ffn` + 7 `.fon`, ที่เหลือเป็นของ expansion
- BMP เป็น 8-bit RLE8 เช่น `Nbhd\NScreen.BMP` 800×600
- `Res_*.RT` + `Res_*.h` (มีทั้งแบบ loose และใน FAR) มีแค่คำสั่ง `INCLUDE`, `BITMAP`, `RLEBMP` จับคู่ resource ID กับ path ของ bitmap **ไม่มีพิกัด**
- การทดลอง H3: เปลี่ยน palette ของ `Nbhd\NScreen.BMP`, `Nbhd\NScreen_Loc.bmp`, `Community\NScreen_unleashed.bmp` เป็นสีชมพู (ขนาดไฟล์เท่าเดิม) วางเป็นไฟล์ loose → พื้นหลัง neighborhood เป็นสีชมพู (ยังไม่ได้แยกว่าไฟล์ไหนคือตัวที่ใช้จริง) ทดสอบเสร็จแล้วลบออก โฟลเดอร์ติดตั้งกลับมาตรงกับ backup

### รูปแบบไฟล์ IFF

- header จริงคือ `IFF FILE 2.5:TYPE FOLLOWED BY SIZE\0 JAMIE DOORNBOS & MAXIS 1` (1,546 ไฟล์) และ `IFF FILE 2.0:…` (32 ไฟล์ house ใน `UserData*`) chunk layout เหมือนกัน
- `tools/iff.py` เดิมเช็ก `TYPE FOLLOWING+SIZE` ซึ่งผิด แก้แล้ว อ่านไฟล์ `.iff` ในโฟลเดอร์ติดตั้งได้ครบ 1,578 ไฟล์

## ผลต่อแผน

1. **ตัวบล็อกหลัก (H4) หายไปแล้ว** เกมรองรับการวาด 1:1 และการขยายแบบจำนวนเต็มเอง
2. **แต่แผน Phase 1 เดิมใช้ไม่ได้ตรงๆ** แผนเดิมคือ "ขยาย bitmap 2× + คูณพิกัด layout 2× ในไฟล์ข้อมูล" แต่พิกัดไม่ได้อยู่ในไฟล์ข้อมูล (H1)
3. `-dpi` ขยายทั้ง UI และโลกในบ้านพร้อมกัน ถ้าใช้ `-dpi:1` เพื่อ UI ความละเอียดสูง โลกในบ้านจะเล็กลงด้วย ถ้าไม่ทำ Phase 4
4. ทางเลือกสำหรับ UI ความละเอียดสูงจริง:
   - **ก. แทน texture ที่ชั้น Vulkan:** layer/proxy DLL ดัก texture ของ UI แล้วแทนด้วยรูป 2× ตาม hash ใช้ได้ก็ต่อเมื่อ UI ถูกส่งเป็น texture แยกและวาดที่ 4K
   - **ข. post-process:** เปลี่ยนการขยายแบบ nearest ของเลเยอร์ UI เป็นตัวขยาย pixel-art (xBRZ/ScaleFX) ทำได้ทุกหน้าจอ แต่ไม่ใช่ HD จริง
   - **ค. แพตช์โค้ด layout ใน exe:** ใหญ่ (ต้อง RE ทีละหน้าจอ และแพตช์ในหน่วยความจำผ่าน proxy DLL เพราะ SteamStub) ความเสี่ยงเรื่องอัปเดตต่ำ เพราะเกมไม่ได้อัปเดตตั้งแต่ 4 เม.ย. 2025
5. **จับเฟรมแล้ว** (ดู "Pipeline การวาดจาก RenderDoc"): ทั้ง ก และ ข เป็นไปได้ ข ง่ายกว่า
6. **ช่องทาง hook:** เกมโหลด Vulkan ด้วย `LoadLibraryA("vulkan-1.dll")` + `GetProcAddress(h, "vkGetInstanceProcAddr")` และไม่ได้ import ฟังก์ชันจำกัดเส้นทางค้น DLL จึงวาง proxy `vulkan-1.dll` (32-bit) ไว้ข้าง exe ได้

## PoC ทาง ข (2026-10-17)

- `vkproxy/` = `vulkan-1.dll` 32-bit วางข้าง `Sims.exe` ส่งต่อทุกคำสั่งไป loader จริง (build ด้วย llvm-mingw + Vulkan SDK)
- เกมดึงฟังก์ชัน `vkCmd*` ผ่าน `vkGetInstanceProcAddr` ก่อนสร้าง device และใช้ render pass แบบเดิม (ไม่ใช้ dynamic rendering, ไม่ใช้ push descriptor)
- pipeline วาด quad ของ UI ใช้ fragment shader `out = สี × texture(binding 0)` (SPIR-V hash `466f913aec289dea`) ตัวเดียวกับที่ใช้ประกอบเลเยอร์ UI ลงจอ
- DLL สร้าง pipeline แฝดที่ใช้ `xbr.frag` และสลับใช้เฉพาะ draw ที่อ่าน render target เล็กกว่า pass ปัจจุบัน ได้ 2 draw ต่อเฟรมในบ้าน (control panel + กล่องข้อความ)
- **ผล:** ทำงานในเกมจริงได้ ปุ่มกลมและตัวหนังสือขอบเนียนขึ้น ขอบโปร่งใสไม่มีขอบเพี้ยน กด F10 เทียบเปิด/ปิดได้ตอนเล่น แต่ไม่ได้เพิ่มรายละเอียด
- ภาพเทียบ: `work/mock/ingame_*_off_vs_xbr*.png` (git-ignored)
- หน้าละแวกบ้าน: รูปแปลงบ้านแต่ละแปลงก็เป็นเลเยอร์แยก (160×86 เป็นต้น) จึงโดน xBR ด้วย ตอนแรกมีเส้นสีเขียวตามขอบแปลงและสีเข้มขึ้น สาเหตุมีสองข้อ: sampler ของ UI ใช้ address mode **REPEAT** (อ่านเลยขอบแล้ววนไปอีกฝั่ง) และรูปแปลงเก็บสีไว้ในพิกเซลที่ alpha = 0 แก้แล้วด้วยการอ่าน texel แบบหนีบพิกัด (`texelFetch` + clamp) และผสมค่าดิบโดยไม่ premultiply
- ขนาด UI บนจอ 4K เลือกได้จาก Resolution + `-dpi`: 2× (3840x2160 + `-dpi:2`), 2.81× (1364x768: เกมวาดที่ 3× แล้วย่อ 0.9375 แบบ linear), 3× (3840x2160 ไม่ใส่ `-dpi`) shader xBR ปรับตาม scale เอง scale ที่ไม่อยู่ในรายการ (เช่น 2.5×) ต้องแพตช์รายการ Resolution ในหน่วยความจำเกม

## Phase 1: วาด UI ที่ความละเอียดจอ + font HD (2026-10-17)

สถานะ: ใช้เล่นจริงได้ เปิดผ่าน Steam ได้โดยไม่ต้องตั้งอะไร (Resolution `3840x2160 (16:9)` ไม่ใส่ `-dpi`) เซฟได้ปกติ

### ขยายเลเยอร์ UI (1b)

- DLL สร้าง render target ทุกตัวที่เล็กกว่า swapchain (เลเยอร์ UI, รูปแปลงบ้าน, tooltip ฯลฯ) ใหญ่ขึ้นเท่ากับสเกล UI ของเกม และขยายทุกอย่างที่วาดลงไป: framebuffer, `renderArea`, viewport/scissor (เก็บค่าที่เกมตั้งไว้ แล้วคูณใหม่ตอนเข้า pass), สี่เหลี่ยมของ `vkCmdClearAttachments` (หนีบให้อยู่ใน render area) เลเยอร์จึงถูกประกอบลงจอแบบ 1:1 แทนการขยาย nearest
- สเกล = ความสูง swapchain ÷ 600 (ปัดลง) ถ้ามี `-dpi:N` ใช้ค่าที่น้อยกว่า ตรงกับสูตรของเกม (ดู "สูตรคำนวณสเกล") `VKPROXY_LAYER_SCALE=N` บังคับค่าได้ `1` = ปิด
- framebuffer ที่ขยายแล้วจะใหญ่เกิน attachment ตัวที่ไม่ได้ขยายไม่ได้ (GPU พัง) จึงขยายเฉพาะเมื่อ attachment ทุกตัวรับขนาดใหม่ได้
- xBR ทำกับ quad ทุกตัวของ pipeline UI (ไอคอนและ bitmap ที่ยังเป็น 1×) shader ใช้ xBR เฉพาะเมื่อ quad ถูกขยายตั้งแต่ 1.5 เท่า (วัดจาก `fwidth`) ถ้าวาด 1:1 (atlas HD, เลเยอร์ที่ขยายแล้ว) จะอ่าน texel ตรงๆ

### Font

- font ของ UI คือ `UIGraphics.far` → `Fonts\variablesans_{07,08,09,10,11,12,14,16,18,20}.ffn` และแบบเงา `_11_s`, `_11_bs`, `_12_bs`, `_14_bs` (+ `_48`, และโฟลเดอร์ภาษา Polish/Russian/…) ตารางชื่อ font ใน exe จับคู่ `kVariableSans` กับ "Comic Sans MS"
- รูปแบบ `.ffn` (อนุมานเอง): `FNTF`, จำนวน glyph u16 ที่ 0x0A, offset ของ bitmap u32 ที่ 0x1C, record 11 ไบต์ต่อ glyph จาก 0x20 (`<HBBHHbbb`: ตัวอักษร, w, h, x, y, left, right, top) แล้ว palette เทา 16 ไบต์ + sheet 4-bit กว้าง 256 → `tools/ffn.py`
- เกมแพ็ก glyph ลง atlas ตอนโหลด: เรียงตามรหัส ข้ามตัวที่ w หรือ h = 0 ขึ้นแถวใหม่เมื่อ x + w > 1024 ระยะขึ้นแถว = glyph ที่สูงที่สุดเท่าที่เคยเจอ (ไม่ reset และนับตัวที่ทำให้ขึ้นแถวด้วย) ความสูง atlas = ขอบล่างต่ำสุด ตรวจกับ atlas ที่ dump จากเกม 10 ตัวแล้ว ตรงทุกพิกเซล ขนาด atlas ไม่ซ้ำกันเลยใช้ขนาดระบุ font ได้: 1023×36 (07), 1018×43 (08), 1020×47 (09), 1018×52 (10), 1005×82 (11), 1020×64 (12), 1017×97 (14), 1022×106 (16), 1020×121 (18), 1022×208 (20), 1024×60 (11_s), 1023×78 (11_bs), 1018×85 (12_bs), 1015×99 (14_bs)
- pixel ใน atlas เป็นขาว premultiplied
- DLL แทน texture ที่ sample ได้ขนาด w×h ด้วย `vkproxy\replace\<w>x<h>@<scale>.bgra` ข้าง DLL (สร้าง image ใหญ่ขึ้น scale เท่า และเปลี่ยนการ upload ทุกครั้งเป็นการ copy ทั้งไฟล์) → `tools/font_atlas.py` สร้างไฟล์เหล่านี้จาก font ของเกม: วาด Comic Sans ใหม่ลงช่องเดิมของแต่ละ glyph ขนาดเดียวทั้ง font ใช้ baseline ร่วม ตัวที่กว้างเกินช่องใช้ `lfWidth` บีบ
- **ต้อง supersample:** รอบแรกให้ GDI วาดตรงๆ (anti-alias ของ GDI มีแค่ ~15 ระดับ และดัดเส้นเข้าตารางพิกเซล) font เล็กสุด (07, ใช้กับป้ายราคา) เส้นหนาไม่เท่ากันจนดูแตก แก้เป็นวาด 4× แล้วเฉลี่ยย่อ (`StretchBlt` HALFTONE) ได้ 255 ระดับ
- ตัวหนังสือในเกมวาดสองแบบ: tooltip/ป้ายราคาเป็นเลเยอร์เล็กของตัวเอง (เช่น 32×15) ส่วนเมนูวงกลม (pie menu) วาดตรงลงภาพประกอบด้วย font 12 สองรอบ (รอบแรกสีเข้มเยื้อง = เงา) ทั้งสองแบบได้ font HD แล้ว
- **Legacy ไม่ใช้ font `_s`/`_bs`/`48`:** เกมโหลด font ปกติทั้ง 10 ขนาดพร้อมกันตอนเปิดเกม และตลอดการเปิดหน้าจอต่างๆ (dump การ upload ทุก texture) ไม่เคยโหลดชุดเงาหรือ 48 ใน exe ชื่อไฟล์ประกอบจาก `"VariableSans_" + "%02d" + ".ffn"` (นำหน้าด้วย `Russian/`/`Polish/` ตามภาษา) ไม่มีส่วนต่อท้าย `_s`/`_bs` สไตล์ `kShadow` ทำด้วยการวาดซ้ำเยื้อง ไฟล์ชุดเงาน่าจะเหลือมาจากเกมต้นฉบับ → font HD ครบแล้วสำหรับภาษาอังกฤษ (ภาษา Polish/Russian ต้องสร้างจากโฟลเดอร์ของภาษานั้นเพิ่ม)

### GPU ค้าง (device lost) — แก้แล้ว

- อาการ: ภาพค้างแต่เสียงและเกมยังเดิน เมาส์ขยับได้ `vkQueueSubmit` คืน -4 และ Event Log มี `nvlddmkm` 153 เกิดเฉพาะตอนเปิด 1b + แทน font เกิดในไม่กี่นาที
- validation layer (Vulkan SDK 1.4 ไม่มี 32-bit ต้องใช้ 1.3.296 + component `com.lunarg.vulkan.32bit`) ไม่เจอ error และ**พอใส่ validation แล้วไม่ค้างเลย** 16 นาที
- สาเหตุ: เกมสร้างและลบ texture font atlas ตลอดเวลา (225 ครั้งใน 16 นาที) driver เอา handle ของ image ที่ถูกลบไปใช้กับ image ใหม่ แต่ DLL ไม่ได้ลบบันทึก "image นี้ต้องแทนด้วย atlas" ออก image ใหม่ (เช่นไอคอน 8×14) จึงโดน copy atlas 3× ทั้งก้อนเกินขอบ validation layer ห่อ handle ให้ไม่ซ้ำกัน บั๊กเลยไม่โผล่ตอนมี validation
- แก้: hook `vkDestroyImage` ลบบันทึกทุกอย่างของ image นั้น, staging buffer ทำครั้งเดียวต่อไฟล์ (เดิมทำใหม่ทุกครั้งที่เกมสร้าง atlas = หน่วยความจำรั่ว), คืนทรัพยากรของ DLL ตอน `vkDestroyDevice` หลังแก้ log เห็น handle ถูกใช้ซ้ำจริง 20 ครั้งใน 1 นาทีแรก และไม่ค้างอีก
- บทเรียน: state ใน proxy ที่ใช้ handle เป็น key ต้องลบตอน destroy เสมอ และอย่าเชื่อผลจาก validation layer เรื่องที่ขึ้นกับการใช้ handle ซ้ำ

### การอ่านภาพกลับตอนเซฟ — แก้แล้ว

- ตอนเซฟ เกมวาดภาพแปลงบ้านลง render target 1360×804 สองตัวกับ 680×400 สองตัว (สี B8G8R8A8 + depth) แล้วอ่านกลับด้วย `vkCmdCopyImageToBuffer` เพื่อทำรูปใน `HouseNN.iff` (`BMP_` 512/513 ขนาด ~224×114 และ 1024/1025 ~112×58 คู่ละรูปสีกับ mask)
- target พวกนี้เล็กกว่าจอจึงโดนขยาย เกมเลยได้แค่มุมซ้ายบน 1/9 → แปลงบ้านหายจากหน้าละแวกบ้านและคลิกไม่ได้ (การคลิกใช้รูปนี้)
- แก้: ถ้าอ่านจาก image ที่ขยาย DLL จะ blit แบบ nearest (ได้พิกเซลกลางของแต่ละบล็อก 3×3 = ภาพที่เกมจะวาดที่ 1×) ลง image 1× ที่ cache ไว้ตามขนาด แล้วให้เกมอ่านจากตัวนั้น validation ไม่เจอ error
- `vkCmdCopyImage`/`vkCmdBlitImage` จาก image ที่ขยายยังแค่ log ไว้ (ยังไม่เคยเจอ)
- สีรูปแปลงบ้าน: ทดสอบ A/B เซฟบ้านเดียวกันสองครั้งติดกัน เปิด/ปิด 1b → สีเฉลี่ยตรงกัน (97,112,67) ต่างเฉพาะ noise ของหญ้า ที่ดูต่างจากเพื่อนบ้านเพราะรูปที่มากับเกม (`TemplateUserData`, เหมือนไฟล์ทุกไบต์) วาดด้วย engine เดิม พอเซฟ Legacy วาดใหม่ด้วย engine ของตัวเอง เป็นแบบนี้แม้ไม่มี mod (ผู้ใช้ลองเกมเปล่าไม่มี DLL แล้ว สีเปลี่ยนแบบเดียวกัน)

### sprite ของ object แบบ HD (เริ่ม Phase 4)

**แปลงจากไฟล์เกมได้ตรงทุกไบต์**
- sprite อยู่ใน `GameData\Objects\Objects.far` และ FAR ของ expansion (chunk SPR2 ในไฟล์ `.iff` ของ object) ทั้งเกมมี 127,939 เฟรม ไม่ซ้ำกัน 85,931 ภาพ
- Legacy ส่ง sprite ขึ้น GPU เป็น B8G8R8A8 แบบ premultiplied: สี palette → RGB565 → คูณ alpha ด้วย `(c*a + 128) / 255` → RGB565 อีกรอบ, alpha = ค่า 5 บิต × 8 (255 ถ้าพิกเซลไม่มี alpha) → `tools/spr2.py` ทำแบบเดียวกัน ตรงกับ upload ของเกมจริง 1,245 จาก 1,245 รูปที่ dump มา จึงสร้างภาพ HD ทั้งเกมจากไฟล์ได้โดยไม่ต้องเปิดเกมไล่เก็บภาพ
- hash ที่ proxy ใช้ตั้งชื่อเป็น FNV-1a 64 แต่ค่าเริ่มต้นเป็น `1469598103934665603` (ค่ามาตรฐานขาดเลข 7 ตัวท้าย) ทุกไฟล์และชื่อ shader ใช้ค่านี้ ห้ามแก้

**การเปลี่ยนรูปใน proxy (content replacement)**
- ตอนเกม upload texture ที่ขนาดตรงกับไฟล์ `<w>x<h>_<hash>@3.bgra` ใน `vkproxy\replace` proxy จะ hash พิกเซล ถ้ามีไฟล์ก็สร้าง image 3× ของตัวเอง แล้วตอนวาดจะผูก descriptor set ที่ชี้ไปที่ image นั้นแทน (เฉพาะ set 0 ที่มี combined image sampler ตัวเดียว) image ของเกมไม่ถูกแตะ รูปที่ไม่มีไฟล์ยังได้ xBR
- staging buffer สำหรับรูปพวกนี้คืนหลัง 16 เฟรม (โปรเซส 32-bit พื้นที่ address ไม่พอถ้าเก็บไว้)
- F9 เปิด/ปิดการเปลี่ยนรูประหว่างเล่น (trigger file `hd.toggle`) ใช้เทียบ

**AI**
- Real-ESRGAN ncnn-vulkan (2022) ทำให้ GPU reset (TDR) บน RTX 5090 ทั้งตอนเกมเปิดและปิด ภาพหลัง reset เป็นขยะ → เปลี่ยนเป็น PyTorch 2.11 + CUDA 12.8 + spandrel (`ai_upscale/`) ไม่มีปัญหา
- เทียบ 4 โมเดลกับ object จริง (`work\cmp\compare_*.png`): x4plus เหมือนภาพวาดและสร้างยาแนวขาวบนกระเบื้องเอง, PBRify V4 ธรรมชาติ (CC0), **UltraSharp V2 คมสุดและซื่อตรง (เลือกใช้)**, DRCT ไม่เดาแต่นุ่ม
- เร็วขึ้นมากเมื่อรวมรูปขนาดใกล้กันเป็น batch (รูปเล็ก รันทีละรูป GPU ใช้แค่ ~25%)

**ประกอบภาพกลับ (`tools/hd_composite.py`) — กฎที่ได้จากการทดสอบในเกม**
1. **coverage ต้องเท่าต้นฉบับเป๊ะ ห้ามเกินแม้ 1 พิกเซล:** shader ของโลกในบ้านอ่านความลึกรายพิกเซลจาก z-buffer texture แยก (ขนาด 1×) ที่นอก sprite เป็นค่าว่าง = หน้าสุด พิกเซลที่รั่วออกนอกขอบจึงวาดทะลุผนัง (เห็นขอบเก้าอี้หลังประตู ขอบตู้เย็นบนเคาน์เตอร์) ขอบ AI ที่นุ่มยังทำให้แผ่นพื้นต่อกันไม่สนิทและพาสีเทาไปทับหญ้า
2. **ตอนนี้จึงไม่ใช้ alpha จาก AI เลย** (และไม่ให้ AI คำนวณ alpha ประหยัดเวลา GPU ราวครึ่ง) ขอบนอกของ object จึงยังเป็นขั้นบันไดเท่าต้นฉบับ **ถ้าจะทำขอบนอกให้เรียบ ต้องทำ z-buffer texture แบบ HD คู่กัน** (proxy เปลี่ยน set 1 ของ pipeline ที่อ่าน z-buffer ด้วย) หรือทำได้แค่ตัดมุมเข้าข้างใน ห้ามขยายออก
3. เติมส่วนโปร่งใสก่อนส่งให้ AI ด้วย**ภาพสะท้อนข้ามขอบ** ไม่ใช่สีเฉลี่ย ไม่อย่างนั้น AI วาดไฮไลต์ตามรอยต่อ
4. สีในส่วนทึบมาจาก AI, ส่วนที่ต้นฉบับโปร่งแสงครึ่งๆ (เงา) ใช้ต้นฉบับเกลี่ย bilinear
- ข้อมูลการวางชิ้น (DGRP) อ่านได้ (v20004) แต่ 1 DGRP = 1 ช่องพื้น และของที่วางติดกันอย่างเคาน์เตอร์เป็น object คนละตัว จึงไม่ใช้ประกอบภาพก่อนส่ง AI
- **พื้น (`floors.iff`) ยังไม่ทำ:** ขยายทีละแผ่นแล้วต่อกันเป็นตาราง ต้องส่งแผ่นที่ต่อเป็นลายให้ AI เห็นข้างเคียง

**ทำเป็นส่วนๆ ตามหมวด** (`tools/hd_art.py --category ...` หมวดจาก OBJD field 38 = FunctionFlags) เกมหลัก: seating 1,728, surfaces 288, appliances 1,185, electronics 2,007, plumbing 1,428, decorative 939, lighting 60, misc 2,917, build 1,661, outdoor 360, other 2,749 เฟรม

### โลกในบ้าน (ข้อมูลสำหรับ Phase 4)

fragment shader ที่วาดโลกในบ้าน (decompile ด้วย `spirv-cross`):

- `63b9db14d8318909`: texture + แสงจุด 2 ดวง (ลดตามระยะ) + `gl_FragDepth` จาก vertex + `discard` ถ้า alpha ≤ 0 → sprite/พื้น/ผนัง
- `0d913d451e5da14f`: สีจาก vertex ไม่มี texture, ความโปร่งใสแบบลายจุด 8×4 จาก `gl_FragCoord` (bitmask)
- `28863a909755e6c1`: สีจาก vertex + `gl_FragDepth`

xBR ทำได้ด้วยหลักเดียวกับ UI แต่ต้องเขียน shader แฝดที่ทำแสง ความลึก และ discard เหมือนเดิม และมีความเสี่ยงรอยต่อระหว่างแผ่นพื้น/ผนัง

### วิธีทดสอบที่ใช้

- เปิด `Sims.exe` ตรงๆ (มี `steam_appid.txt`) พร้อม env var ของ DLL, log ที่ `%VKPROXY_DIR%\vkproxy.log` (ค่าเริ่มต้น `%TEMP%\vkproxy`)
- validation: `VK_LAYER_PATH=C:\VulkanSDK\1.3.296.0\Bin32`, `VK_INSTANCE_LAYERS=VK_LAYER_KHRONOS_validation`, `VK_LAYER_SETTINGS_PATH=<ไฟล์>` ที่มี `khronos_validation.debug_action = VK_DBG_LAYER_ACTION_LOG_MSG` และ `khronos_validation.log_filename = <ไฟล์ log>`
- trace เฟรม: F11 ในเกม หรือสร้างไฟล์ `trace.now` ในโฟลเดอร์ log (ห้ามส่งปุ่มเข้าเกมจากสคริปต์ เพราะจะไปโดนหน้าต่างอื่นที่ focus อยู่)
- ก่อนให้เซฟระหว่างทดสอบ สำรอง `UserData` ทั้งโฟลเดอร์ แล้วกู้คืนด้วย `robocopy /MIR` และตรวจขนาด/เวลาไฟล์ทุกไฟล์

## วิธีทำซ้ำ (เมื่อ EA อัปเดตเกม)

1. `python tools\inventory.py <game> -o work\inventory.md` และ `python tools\exe_strings.py <game>\Sims.exe -o work\strings.md`
2. เปิดเกม แล้ว dump ทั้ง image ของ `Sims.exe` จากหน่วยความจำ (`OpenProcess` + `ReadProcessMemory` ตั้งแต่ module base ยาว `SizeOfImage`) เพราะ `.text` ในไฟล์ถูกเข้ารหัส
3. หา string (`dpi`, `monitor`, `maxaspect`) ใน dump แล้วหา dword ใน `.text` ที่เท่ากับ VA ของ string (VA = base + RVA)
4. disassemble รอบจุดอ้างอิง: `objdump -D -b binary -m i386 -M intel --adjust-vma=<base> --start-address=… --stop-address=… dump.bin`

สคริปต์ dump/หา xref ของรอบนี้อยู่นอก repo ถ้าต้องใช้ซ้ำบ่อยควรย้ายเข้า `tools/` พร้อม unittest

## ไฟล์ที่ได้ (git-ignored ไม่ commit)

- `backup\The Sims Legacy Collection\` สำเนาโฟลเดอร์ติดตั้ง (10,406 ไฟล์, 3.4 GB)
- `backup\SavedGames-The Sims 25\` สำเนาข้อมูลผู้เล่น (7,122 ไฟล์, 544 MB) สำรองหลังการเปิดเกมทดสอบครั้งแรก
- `work\inventory.md`, `work\strings*.md`, `work\far_uigraphics.txt`
- `work\Sims_mem.bin` (memory dump), `work\dis_*.txt` (disassembly)
- `work\vkproxy\` log, shader ที่ dump, `replace\` (atlas HD), `vl\` (validation), log ของรอบที่ค้าง (`crash_*.log`)
- `work\fonts\` (.ffn ที่แตกจาก FAR), `work\mock\` (ภาพเทียบ), `work\thumbs\`, `work\ab\` (รูปแปลงบ้านที่ใช้เทียบ)
- `work\save_backup_*`, `work\save_*` สำเนาเซฟก่อน/หลังการทดสอบแต่ละรอบ
