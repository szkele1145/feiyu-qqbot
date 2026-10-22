; Inno Setup 脚本 —— 生成 feiyu-qqbot 安装程序
; 编译： ISCC.exe packaging\installer.iss
; 或用： python packaging\build_installer.py

#define AppName "feiyu-qqbot"
#define AppVersion "0.1.0"
#define AppPublisher "feiyu-qqbot"
#define AppURL "https://github.com/feiyu-qqbot/feiyu-qqbot"
#define AppExeName "feiyu-qqbot.exe"

[Setup]
AppId={{8E7C2A1B-4F3D-4C5E-9A2B-1D6E8F3A5B7C}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=..\dist\installer
OutputBaseFilename=feiyu-qqbot-setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; 安装到用户目录，无需管理员权限（与 HKCU 开机自启保持一致）
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
UninstallDisplayIcon={app}\{#AppExeName}
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "chinese"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[CustomMessages]
chinese.WelcomeLabel1=安装 [name]（QQ 官方机器人 AI Agent）
chinese.WelcomeLabel2=[name] 将安装到以下文件夹：%n%n它将在 QQ 开放平台注册的后台运行，并开放本地 WebUI 面板。%n若尚未创建机器人，请先到 q.qq.com 申请。
chinese.FinishedLabelNoIcons=安装完成。
chinese.ClickFinish=要立即启动 [name] 吗？

[Files]
; 主程序（打包产物由 build.py 生成到 dist\feiyu-qqbot\）
Source: "..\dist\feiyu-qqbot\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; 文档
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion isreadme
; 素材库空目录
Source: "..\assets\voices\*"; DestDir: "{app}\assets\voices"; Flags: ignoreversion skipifsourcedoesntexist
Source: "..\assets\images\*"; DestDir: "{app}\assets\images"; Flags: ignoreversion skipifsourcedoesntexist

[Dirs]
Name: "{app}\assets\voices"; Flags: uninsneveruninstall
Name: "{app}\assets\images"; Flags: uninsneveruninstall
Name: "{app}\logs"; Flags: uninsneveruninstall
Name: "{app}\data"; Flags: uninsneveruninstall

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\卸载 {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"
Name: "autostart"; Description: "开机自动启动（后台运行机器人）"; GroupDescription: "附加任务:"; Flags: unchecked

[Registry]
; 开机自启写入 HKCU（当前用户），故用 lowest 权限安装模式
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
    ValueType: string; ValueName: "{#AppName}"; ValueData: """{app}\{#AppExeName}"""; \
    Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\{#AppExeName}"; Description: "立即启动 {#AppName}"; \
    Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\logs"
Type: filesandordirs; Name: "{app}\data"

[UninstallRun]
; 停止可能还在运行的实例
Filename: "{app}\{#AppExeName}"; Parameters: "--stop"; Flags: runhidden; RunOnceId: "StopApp"
