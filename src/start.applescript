use framework "Foundation"
use framework "AppKit"
use framework "CoreImage"
use scripting additions

property statusItem : missing value
property theMenu : missing value
property addrText : "http://localhost:8765/"
property lastItem : missing value
property portItem : missing value
property pendingItem : missing value
property clipWriteItem : missing value
property clipReadItem : missing value
property inboxPath : ""
property outboxPath : ""
property flagsPath : ""
property langFile : ""
property langZh : true
property qrWindow : missing value
property resDir : "."
property serverPort : 8765
property runtimeFile : ""

on run argv
    -- 启动器把 Resources 目录作为参数传入（换 Wi-Fi、换路径都不影响）
    if (count of argv) > 0 then set resDir to item 1 of argv
    set runtimeFile to (do shell script "echo $HOME") & "/.androidtransfer/runtime"
    set inboxPath to (do shell script "echo $HOME") & "/Downloads/android-inbox"
    set outboxPath to (do shell script "echo $HOME") & "/Downloads/android-outbox"
    set flagsPath to (do shell script "echo $HOME") & "/.androidtransfer/flags"
    set langFile to flagsPath & "/lang"
    -- 发件箱就是"拖进去即发送"的那个目录，所以这里必须保证它存在
    do shell script "mkdir -p " & quoted form of outboxPath & " " & quoted form of flagsPath

    -- 界面语言：~/.androidtransfer/lang 优先，缺省跟随系统首选语言
    set langZh to my detectLang()

    -- 服务端可能在首选端口被占用时自动顺延，端口从运行时文件读，不要写死
    set serverPort to my readPort(50)
    set addrText to my fetchAddr()

    set statusItem to current application's NSStatusBar's systemStatusBar's statusItemWithLength:-1.0
    set btn to statusItem's button()

    -- 菜单栏图标：与应用图标同源的迷你磁贴（渐变圆角方块 + 白色下箭头）
    set menuImg to missing value
    try
        set menuImg to my loadMenuIcon(resDir & "/MenuIcon.png", resDir & "/MenuIcon@2x.png", 18)
    end try
    if menuImg is not missing value then
        set iconStyle to "tile"
        try
            set iconStyle to do shell script "cat " & quoted form of (resDir & "/MenuIcon.style") & " 2>/dev/null"
        end try
        -- 只有透明底的单色图才该当模板图；彩色磁贴若设成模板会被压成纯黑方块
        if iconStyle is "mono" then menuImg's setTemplate:true
        btn's setImage:menuImg
        btn's setTitle:""
    else
        -- 回退：系统 SF Symbol
        set symImg to current application's NSImage's imageWithSystemSymbolName:"arrow.down.to.line" accessibilityDescription:(my L("文件互传", "FileRelay"))
        if symImg is not missing value then
            symImg's setTemplate:true
            btn's setImage:symImg
            btn's setTitle:""
        else
            btn's setTitle:"⬇"
        end if
    end if

    my buildMenu()

    my tick:(missing value)   -- 先刷一次，别让菜单栏空等 2 秒

    current application's NSTimer's scheduledTimerWithTimeInterval:2.0 target:me selector:"tick:" userInfo:(missing value) repeats:true

    (current application's NSApplication's sharedApplication())'s performSelector:"run"
end run

-- ---------------------------------------------------------------- 界面语言

-- 单条文案的中英对照入口：L(中文, English)。语言状态只看 langZh。
on L(z, e)
    if langZh then return z
    return e
end L

on detectLang()
    -- 用户显式选过的语言存在 ~/.androidtransfer/lang（switchLang: 写入）
    try
        set v to do shell script "cat " & quoted form of langFile & " 2>/dev/null"
        if v is "en" then return false
        if v is "zh" then return true
    end try
    -- 没选过就跟系统首选语言走
    try
        set p to (current application's NSLocale's preferredLanguages()'s firstObject()) as text
        if p does not start with "zh" then return false
    end try
    return true
end detectLang

on writeLang()
    set v to "en"
    if langZh then set v to "zh"
    do shell script "printf %s " & v & " > " & quoted form of langFile
end writeLang

-- 菜单整个建一遍：语言切换时也走这里（把 statusItem 的菜单换成新的）
on buildMenu()
    set theMenu to current application's NSMenu's alloc()'s initWithTitle:""

    -- 主操作放最上面：点一下弹二维码，手机直接扫
    theMenu's addItem:(my mkItem(my L("显示二维码", "Show QR Code"), "showQR:", "qrcode"))

    theMenu's addItem:(current application's NSMenuItem's separatorItem())

    -- Mac → 手机：把东西放进发件箱，手机打开页面就取走（异步队列，不是推送）
    theMenu's addItem:(my mkItem(my L("发送剪贴板文字到手机", "Send Clipboard Text to Phone"), "sendClip:", "doc.on.clipboard"))
    theMenu's addItem:(my mkItem(my L("发送文件到手机…", "Send Files to Phone…"), "sendFiles:", "paperplane"))
    theMenu's addItem:(my mkItem(my L("打开发送目录", "Open Outbox Folder"), "openOutbox:", "tray.full"))
    theMenu's addItem:(my mkItem(my L("清空发件箱…", "Clear Outbox…"), "clearOutbox:", "trash"))

    set pendingItem to my mkItem(my L("待发 —", "Outbox —"), missing value, "arrow.up.circle")
    theMenu's addItem:pendingItem

    theMenu's addItem:(current application's NSMenuItem's separatorItem())

    set openItem to my mkItem(my L("打开接收目录", "Open Inbox Folder"), "openInbox:", "folder")
    theMenu's addItem:openItem

    set copyItem to my mkItem(my L("复制接收地址", "Copy Inbox Address"), "copyAddr:", "link")
    theMenu's addItem:copyItem

    set lastItem to my mkItem(my L("最近收到：—", "Last received: —"), missing value, "clock")
    theMenu's addItem:lastItem

    -- 端口被占用时会自动顺延，把实际端口显示出来，避免与预期不一致时无从排查
    set portItem to my mkItem(my L("服务端口：", "Port: ") & (serverPort as text), missing value, "network")
    theMenu's addItem:portItem

    theMenu's addItem:(current application's NSMenuItem's separatorItem())

    -- 开关靠哨兵文件表达（服务端每次请求现读），所以改完立刻生效、不用重启服务
    set clipWriteItem to my mkItem(my L("手机文字自动写入剪贴板", "Auto-copy phone text to clipboard"), "toggleClipWrite:", "clipboard")
    theMenu's addItem:clipWriteItem

    set clipReadItem to my mkItem(my L("允许手机读取 Mac 剪贴板", "Allow phone to read Mac clipboard"), "toggleClipRead:", "eye")
    theMenu's addItem:clipReadItem

    -- 传文字历史只滚动保留最近 200 条（服务端负责裁剪），这里提供一键清空的入口
    theMenu's addItem:(my mkItem(my L("清空文字记录…", "Clear Text History…"), "clearClipLog:", "eraser"))

    theMenu's addItem:(current application's NSMenuItem's separatorItem())

    -- 语言切换：菜单项文案永远显示"要切换到"的那个语言
    theMenu's addItem:(my mkItem(my L("Switch to English", "切换到中文"), "switchLang:", "globe"))

    theMenu's addItem:(my mkItem(my L("使用说明", "User Guide"), "openDoc:", "book"))

    set quitItem to my mkItem(my L("退出", "Quit"), "quitApp:", "power")
    quitItem's setKeyEquivalent:"q"
    theMenu's addItem:quitItem

    statusItem's setMenu:theMenu
end buildMenu

on switchLang:sender
    set langZh to not langZh
    my writeLang()
    my buildMenu()
    -- 二维码窗口的文案是建窗时写死的，丢弃旧窗让下次按新语言重建
    if qrWindow is not missing value then
        qrWindow's orderOut:me
        set qrWindow to missing value
    end if
    my notifyUser(my L("界面语言已切换为中文", "UI language switched to English"))
end switchLang:

-- ---------------------------------------------------------------- 端口 / 地址

on baseURL()
    return "http://127.0.0.1:" & (serverPort as text)
end baseURL

on portFromFile()
    -- 运行时文件形如：PORT=8766 / PID=123 / URL=http://...
    try
        set p to do shell script "sed -n 's/^PORT=//p' " & quoted form of runtimeFile & " 2>/dev/null | head -1"
        if p is not "" then return (p as integer)
    end try
    return 0
end portFromFile

on readPort(tries)
    -- 启动器先起服务、后拉起菜单栏，服务绑定端口需要一点时间，这里轮询等它
    repeat tries times
        set p to my portFromFile()
        if p > 0 then return p
        delay 0.1
    end repeat
    return serverPort
end readPort

on fetchAddr()
    try
        return do shell script "curl -s --noproxy '*' --max-time 3 " & my baseURL() & "/addr"
    end try
    return addrText
end fetchAddr

-- ---------------------------------------------------------------- 菜单栏图标

on loadMenuIcon(p1x, p2x, pt)
    -- 手动组 1x/2x 两个 rep 并都声明为 pt 点，Retina 下系统才会挑 36px 那份
    set im to current application's NSImage's alloc()'s init()
    repeat with p in {p1x, p2x}
        try
            set d to current application's NSData's dataWithContentsOfFile:(p as text)
            if d is not missing value then
                set r to current application's NSBitmapImageRep's alloc()'s initWithData:d
                if r is not missing value then
                    r's setSize:{pt, pt}
                    im's addRepresentation:r
                end if
            end if
        end try
    end repeat
    if ((im's |representations|()'s |count|()) as integer) = 0 then return missing value
    im's setSize:{pt, pt}
    return im
end loadMenuIcon

-- ---------------------------------------------------------------- 定时刷新

on tick:sender
    -- 服务重启可能落到别的端口，跟着运行时文件走
    set p to my portFromFile()
    if p > 0 then set serverPort to p
    try
        set out to do shell script "curl -s --noproxy '*' --max-time 2 " & my baseURL() & "/status"
        set pend to my field(out, "PENDING=")
        set online to my field(out, "ONLINE=")
        set cwrite to my field(out, "CLIPWRITE=")
        set cread to my field(out, "CLIPREAD=")
        set lkind to my field(out, "LASTKIND=")
        set lname to my field(out, "LASTNAME=")
        set ltime to my field(out, "LASTTIME=")

        if pend is not "" then
            if langZh then
                if online is "1" then
                    pendingItem's setTitle:("待发 " & pend & " 项 · 手机在线")
                else
                    pendingItem's setTitle:("待发 " & pend & " 项 · 手机离线")
                end if
            else
                if online is "1" then
                    pendingItem's setTitle:("Outbox: " & pend & " · phone online")
                else
                    pendingItem's setTitle:("Outbox: " & pend & " · phone offline")
                end if
            end if
        end if

        if lkind is "" then
            lastItem's setTitle:(my L("最近收到：—", "Last received: —"))
        else if lkind is "file" then
            lastItem's setTitle:((my L("最近收到：", "Last received: ")) & lname & " @ " & ltime)
        else
            lastItem's setTitle:((my L("最近收到文字 @ ", "Last received text @ ")) & ltime)
        end if

        if cwrite is not "" then clipWriteItem's setState:(my onOff(cwrite))
        if cread is not "" then clipReadItem's setState:(my onOff(cread))
    end try
    try
        portItem's setTitle:((my L("服务端口：", "Port: ")) & (serverPort as text))
    end try
end tick:

on field(txt, key)
    -- 从 "KEY=value" 逐行的状态快照里取一个值；没有就返回空串
    -- 两个实测踩过的坑，改这个函数前先读一遍：
    --   1) 变量别叫 lines / line：它们是 AppleScript 的保留术语，`set lines to ...` 会被
    --      解析成 "set every line"，直接报 -10006；
    --   2) 切行只能用 paragraphs，**不能**按 linefeed 切 —— `do shell script` 回来的
    --      换行是 CR 而非 LF（实测 `(out contains linefeed)` 为 false），按 linefeed 切
    --      等于没切，整段快照会被当成"一行"返回，菜单栏状态行显示成一坨。
    --      这条尤其阴：用 linefeed 手工拼的测试数据能过，真实数据却挂。
    set res to ""
    set AppleScript's text item delimiters to key
    repeat with rowItem in (paragraphs of txt)
        set rowText to rowItem as text
        if rowText starts with key then
            set theParts to text items of rowText
            if (count of theParts) >= 2 then set res to (item 2 of theParts)
            exit repeat
        end if
    end repeat
    set AppleScript's text item delimiters to ""
    return res
end field

on onOff(v)
    if v is "1" then return 1
    return 0
end onOff

on stamp()
    return do shell script "date +%Y%m%d-%H%M%S"
end stamp

on notifyUser(msg)
    try
        set nf to current application's NSUserNotification's alloc()'s init()
        nf's setTitle:(my L("文件互传", "FileRelay"))
        nf's setInformativeText:msg
        current application's NSUserNotificationCenter's defaultUserNotificationCenter()'s deliverNotification:nf
    end try
end notifyUser

-- ---------------------------------------------------------------- 菜单动作

-- -------- 发送（Mac → 手机）：写进发件箱就完事，手机打开页面自己来取 --------
-- 局域网 http 下浏览器没有被动接收的通道，所以这里只"投递"，不假装推送。

on sendClip:sender
    set t to ""
    try
        set t to the clipboard as text
    end try
    if t is "" or t is missing value then
        my notifyUser(my L("剪贴板里没有文字", "The clipboard has no text"))
        return
    end if
    do shell script "mkdir -p " & quoted form of outboxPath
    set nm to (my L("剪贴板-", "Clipboard-")) & my stamp() & ".txt"
    -- 用 Foundation 直接写文件：绕开 shell 与编码问题，中文内容原样落盘
    set nsStr to current application's NSString's stringWithString:t
    set okFlag to nsStr's writeToFile:(outboxPath & "/" & nm) atomically:true encoding:(current application's NSUTF8StringEncoding) |error|:(missing value)
    if okFlag then
        my notifyUser((my L("已排队：", "Queued: ")) & nm & (my L("（手机打开页面即取）", " (the phone picks it up when it opens the page)")))
    else
        my notifyUser((my L("写入发件箱失败，检查 ", "Failed to write to the outbox, check ")) & outboxPath)
    end if
end sendClip:

on sendFiles:sender
    do shell script "mkdir -p " & quoted form of outboxPath
    set panel to current application's NSOpenPanel's openPanel()
    panel's setAllowsMultipleSelection:true
    panel's setCanChooseFiles:true
    panel's setCanChooseDirectories:false
    panel's setTitle:(my L("选择要发到手机的文件", "Choose Files to Send to the Phone"))
    panel's setMessage:(my L("选中的文件会进入发件箱，手机打开页面即可取走", "Selected files go into the outbox; the phone picks them up when it opens the page"))
    panel's setPrompt:(my L("放入发件箱", "Add to Outbox"))
    current application's NSApplication's sharedApplication()'s activateIgnoringOtherApps:true
    if (panel's runModal()) is not 1 then return   -- 1 = NSModalResponseOK

    -- 变量别叫 urls：AppleScript 标识符大小写不敏感，会和 NSOpenPanel 的 URLs() 撞成同一个词
    set selectedFiles to panel's URLs()
    set fm to current application's NSFileManager's defaultManager()
    set n to 0
    set firstName to ""
    repeat with pickedFile in selectedFiles
        set srcPath to (pickedFile's |path|()) as text
        set base to (pickedFile's lastPathComponent()) as text
        set dst to outboxPath & "/" & base
        -- 同名直接覆盖：发件箱的语义是"最新这份要发过去"。堆 (1)(2) 副本只会让手机上
        -- 冒出一串看起来一样的条目；覆盖会更新 mtime，服务端据此重新排到队列最前
        fm's removeItemAtPath:dst |error|:(missing value)
        if (fm's copyItemAtPath:srcPath toPath:dst |error|:(missing value)) then
            set n to n + 1
            if firstName is "" then set firstName to base
        end if
    end repeat

    if n is 0 then
        my notifyUser(my L("没有文件进入发件箱", "No files were added to the outbox"))
    else if n is 1 then
        my notifyUser((my L("已排队：", "Queued: ")) & firstName & (my L("（手机打开页面即取）", " (the phone picks it up when it opens the page)")))
    else
        my notifyUser((my L("已排队：", "Queued: ")) & (n as text) & (my L(" 个文件（手机打开页面即取）", " file(s) (the phone picks them up when it opens the page)")))
    end if
end sendFiles:

on openOutbox:sender
    do shell script "mkdir -p " & quoted form of outboxPath
    do shell script "open " & quoted form of outboxPath
end openOutbox:

on clearOutbox:sender
    set fm to current application's NSFileManager's defaultManager()
    -- 变量别叫 items：items 是 AppleScript 的复数保留术语，命名上避开它
    set nameList to fm's contentsOfDirectoryAtPath:outboxPath |error|:(missing value)
    if nameList is missing value then
        my notifyUser(my L("发件箱还是空的", "The outbox is empty"))
        return
    end if
    set n to 0
    repeat with fileName in nameList
        if not ((fileName as text) starts with ".") then set n to n + 1
    end repeat
    if n is 0 then
        my notifyUser(my L("发件箱还是空的", "The outbox is empty"))
        return
    end if

    set alert to current application's NSAlert's alloc()'s init()
    alert's setMessageText:(my L("清空发件箱？", "Clear the outbox?"))
    alert's setInformativeText:((my L("将从发件箱删掉 ", "This will delete ")) & (n as text) & (my L(" 项待发内容；手机上已经取走的副本不受影响。", " queued item(s) from the outbox; copies already picked up on the phone are not affected.")))
    alert's addButtonWithTitle:(my L("删掉", "Delete"))
    alert's addButtonWithTitle:(my L("取消", "Cancel"))
    if (alert's runModal()) is not 1000 then return   -- 1000 = NSAlertFirstButtonReturn

    set gone to 0
    repeat with fileName in nameList
        -- 逐个 removeItem，不做递归删除：这里只该有文件，不该有子目录
        if (fm's removeItemAtPath:(outboxPath & "/" & (fileName as text)) |error|:(missing value)) then set gone to gone + 1
    end repeat
    my notifyUser((my L("已清空发件箱（", "Outbox cleared (")) & (gone as text) & (my L(" 项）", " item(s)")))
end clearOutbox:

on clearClipLog:sender
    -- 传文字历史 = 收件目录里的 clipboard.log（服务端滚动保留最近 200 条）。
    -- 清空走覆写为空文件，不删文件本身，服务端下次照常追加。
    set logFile to inboxPath & "/clipboard.log"
    set fm to current application's NSFileManager's defaultManager()
    if (fm's fileExistsAtPath:logFile) as boolean is false then
        my notifyUser(my L("还没有文字记录", "No text history yet"))
        return
    end if
    set alert to current application's NSAlert's alloc()'s init()
    alert's setMessageText:(my L("清空文字记录？", "Clear text history?"))
    alert's setInformativeText:(my L("将清掉手机传来的全部文字历史（可能包含复制过的密码、验证码）。此操作不可恢复。", "This clears all text history sent from the phone (possibly including copied passwords and verification codes). This cannot be undone."))
    alert's addButtonWithTitle:(my L("清空", "Clear"))
    alert's addButtonWithTitle:(my L("取消", "Cancel"))
    if (alert's runModal()) is not 1000 then return   -- 1000 = NSAlertFirstButtonReturn
    do shell script "printf '' > " & quoted form of logFile
    my notifyUser(my L("已清空文字记录", "Text history cleared"))
end clearClipLog:

-- -------- 两个开关：用哨兵文件表达，服务端每次请求现读，改完立刻生效不用重启 --------

on toggleClipWrite:sender
    set f to flagsPath & "/no_clip_write"
    if (sender's state()) is 1 then
        do shell script "touch " & quoted form of f        -- 关：手机发来的文字不再写进剪贴板
        sender's setState:0
        my notifyUser(my L("已关闭：手机文字不再自动写入剪贴板", "Off: incoming phone text no longer auto-copies to the clipboard"))
    else
        do shell script "rm -f " & quoted form of f
        sender's setState:1
        my notifyUser(my L("已开启：手机文字会自动写入剪贴板，Mac 上直接 Cmd+V", "On: incoming phone text auto-copies to the clipboard — just Cmd+V on the Mac"))
    end if
end toggleClipWrite:

on toggleClipRead:sender
    set f to flagsPath & "/clip_read"
    if (sender's state()) is 1 then
        do shell script "rm -f " & quoted form of f
        sender's setState:0
        my notifyUser(my L("已关闭：手机不能读取 Mac 剪贴板", "Off: the phone cannot read the Mac clipboard"))
    else
        do shell script "touch " & quoted form of f
        sender's setState:1
        -- 剪贴板里可能有密码，所以默认是关的，开启时把风险说清楚
        my notifyUser(my L("已开启：局域网内拿到地址的手机可以读取 Mac 剪贴板", "On: a phone with the address on the LAN can read the Mac clipboard"))
    end if
end toggleClipRead:

on openInbox:sender
    do shell script "open " & quoted form of inboxPath
end openInbox:

on copyAddr:sender
    set addrText to my fetchAddr()
    set the clipboard to addrText
    set nf to current application's NSUserNotification's alloc()'s init()
    nf's setTitle:(my L("文件互传", "FileRelay"))
    nf's setInformativeText:((my L("已复制地址: ", "Address copied: ")) & addrText)
    current application's NSUserNotificationCenter's defaultUserNotificationCenter()'s deliverNotification:nf
end copyAddr:

on showQR:sender
    -- 每次弹出前重新取一次地址（换 Wi-Fi 后 IP 会变）
    set addrText to my fetchAddr()
    set serverPort to my readPort(5)

    if qrWindow is not missing value then
        qrWindow's makeKeyAndOrderFront:me
        current application's NSApplication's sharedApplication()'s activateIgnoringOtherApps:true
        return
    end if

    -- 优先向服务端要高清位图二维码：每个模块是若干个真实像素，放大到几百像素依然锐利。
    -- （系统 CoreImage 生成的码只有「1 模块 = 1 像素」，放大后模块边界会发虚）
    set qrImg to missing value
    set tmpQR to "/tmp/androidtransfer_qr.png"
    try
        do shell script "rm -f " & tmpQR & "; curl -s --noproxy '*' --max-time 6 -o " & tmpQR & " " & my baseURL() & "/qr.png"
        set candidate to current application's NSImage's alloc()'s initWithContentsOfFile:tmpQR
        if candidate is not missing value then
            set sz to candidate's |size|()
            -- 注意：NSSize 在 AppleScriptObjC 里返回的是 record {width, height}，
            -- 不是 list，取 item 1 会直接抛错（-1700）——必须用 width of。
            if (width of sz) > 100 then set qrImg to candidate
        end if
    end try

    -- 回退：服务未就绪时用系统 CoreImage 本地生成
    if qrImg is missing value then
        set nsStr to current application's NSString's stringWithString:addrText
        set theData to nsStr's dataUsingEncoding:(current application's NSUTF8StringEncoding)
        set f to current application's CIFilter's filterWithName:"CIQRCodeGenerator"
        f's setValue:theData forKey:"inputMessage"
        f's setValue:"M" forKey:"inputCorrectionLevel"
        set ciImg to f's outputImage
        set rep to current application's NSCIImageRep's imageRepWithCIImage:ciImg
        set qrImg to current application's NSImage's alloc()'s initWithSize:(rep's |size|())
        qrImg's addRepresentation:rep
    end if
    qrImg's setImageInterpolation:1 -- 最近邻：二维码模块边界保持锐利

    set w to current application's NSWindow's alloc()'s initWithContentRect:{{0, 0}, {420, 540}} styleMask:7 backing:2 defer:false
    w's setReleasedWhenClosed:false -- 关掉后仍保留引用，便于复用
    w's setTitle:(my L("扫码连接 · 文件互传", "Scan to Connect · FileRelay"))
    set whiteCol to current application's NSColor's whiteColor()
    w's setBackgroundColor:whiteCol
    w's |center|() -- center 是 AppleScript 保留术语，必须管道转义
    set cv to w's contentView()

    -- 服务端产出的 PNG 自带 4 个模块的 quiet zone，这里直接铺满显示区即可
    set iv to current application's NSImageView's alloc()'s initWithFrame:{{30, 150}, {360, 360}}
    iv's setImage:qrImg
    iv's setImageScaling:3
    cv's addSubview:iv

    set f13 to current application's NSFont's systemFontOfSize:13
    set tf to current application's NSTextField's alloc()'s initWithFrame:{{20, 108}, {380, 24}}
    tf's setStringValue:addrText
    tf's setBezeled:false
    tf's setDrawsBackground:false
    tf's setEditable:false
    tf's setSelectable:true
    tf's setAlignment:1
    tf's setFont:f13
    cv's addSubview:tf

    set grayCol to current application's NSColor's secondaryLabelColor()
    set f12 to current application's NSFont's systemFontOfSize:12
    set hint to current application's NSTextField's alloc()'s initWithFrame:{{20, 70}, {380, 20}}
    hint's setStringValue:(my L("手机与 Mac 连同一 Wi-Fi，用相机或微信扫一扫", "The phone is on the same Wi-Fi as the Mac — scan with Camera or WeChat"))
    hint's setBezeled:false
    hint's setDrawsBackground:false
    hint's setEditable:false
    hint's setSelectable:false
    hint's setAlignment:1
    hint's setTextColor:grayCol
    hint's setFont:f12
    cv's addSubview:hint

    set qrWindow to w
    w's setLevel:3 -- 浮在其他窗口之上，避免被挡
    w's makeKeyAndOrderFront:me
    current application's NSApplication's sharedApplication()'s activateIgnoringOtherApps:true
end showQR:

on openDoc:sender
    -- 手册跟随菜单栏语言
    if langZh then
        do shell script "open " & quoted form of (resDir & "/使用手册.html")
    else
        do shell script "open " & quoted form of (resDir & "/manual-en.html")
    end if
end openDoc:

on quitApp:sender
    do shell script "pkill -f 'Resources/server.py'"
    current application's NSApplication's sharedApplication()'s terminate:me
end quitApp:

-- 构造带 SF Symbol 图标的菜单项
on mkItem(t, act, sym)
    set mi to (current application's NSMenuItem's alloc()'s initWithTitle:t action:act keyEquivalent:"")
    if act is not missing value then mi's setTarget:me
    try
        if sym is not missing value then
            set im to current application's NSImage's imageWithSystemSymbolName:sym accessibilityDescription:t
            if im is not missing value then mi's setImage:im
        end if
    end try
    return mi
end mkItem
