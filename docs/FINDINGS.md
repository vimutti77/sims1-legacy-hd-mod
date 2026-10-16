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

วัดจาก screenshot ที่ `-dpi:2` ว่าบล็อก 2×2 แต่ละบล็อกเป็นสีเดียวกันหรือไม่:

- control panel และพื้นหลังเทา: บล็อกไม่สม่ำเสมอ 0% → วาดที่ความละเอียดตรรกะแล้วขยายแบบ nearest
- หญ้าและตัวบ้าน: บล็อกไม่สม่ำเสมอ 88–94% → วาดที่ความละเอียดจริงของจอ (4K) ด้วยการกรองแบบนุ่ม

แปลว่าเกมประกอบภาพที่ 4K และวาด UI กับโลกคนละทาง แต่ยังไม่รู้ว่า UI bitmap แต่ละรูปถูกอัปโหลดเป็น Vulkan texture แยก (hook ได้) หรือถูกรวมเป็นเลเยอร์เดียวก่อน ต้องจับเฟรมด้วย RenderDoc ถึงจะรู้

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
   - **ค. แพตช์โค้ด layout ใน exe:** ใหญ่และเปราะ (SteamStub ต้องแพตช์ในหน่วยความจำผ่าน proxy DLL และพังได้ทุกครั้งที่ EA อัปเดต)
5. **ขั้นต่อไปที่แนะนำ:** จับเฟรมด้วย RenderDoc ที่ `-dpi:2` ดูว่า UI ถูกส่งไป GPU อย่างไร ผลจะตัดสินว่าทาง ก. เป็นไปได้หรือไม่

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
