; Установщик StreamVault для Windows (Inno Setup 6).
;
; Собирается из build\dist\StreamVault, который готовит packaging\build.py, —
; вызывать его напрямую не нужно: python packaging\release.py <версия>
; собирает программу, архив и установщик. Версия приходит ключом
; /DAppVersion — из streamvault/__init__.py, как у архива.
;
; Ставится по умолчанию для одного пользователя (без запроса прав
; администратора) в %LOCALAPPDATA%\Programs\StreamVault; папку можно выбрать.
; Кто хочет для всех — выбирает это в первом окне установщика; тогда
; настройки и журналы программа хранит в %APPDATA%\StreamVault (в Program
; Files ей писать нельзя).

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

#define AppName "StreamVault"
#define AppExe "StreamVault.exe"
#define AppUrl "https://github.com/Hxrmfull/StreamVault"
#define SourceDir "..\build\dist\StreamVault"

[Setup]
; Постоянный идентификатор: по нему новая версия ставится поверх старой,
; а не рядом. Менять нельзя.
AppId={{48CB40AF-E321-4A9A-BD94-17CBA0F4153A}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppName}
AppPublisherURL={#AppUrl}
AppSupportURL={#AppUrl}/issues
AppUpdatesURL={#AppUrl}/releases
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=no
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename={#AppName}-{#AppVersion}-setup
SetupIconFile=..\build\StreamVault.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName} {#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; Программа может быть запущена во время обновления — предлагаем закрыть.
CloseApplications=yes
; После установки Проводник обновляет иконки: иначе ярлыки показывают старую
; иконку из кэша — exe заменён по тому же пути, и Windows этого не замечает.
ChangesAssociations=yes
RestartApplications=no

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; Библиотеки прошлой версии — целиком: лишние файлы в _internal ни к чему.
; Настройки, журналы и скачанные кнопками инструменты лежат рядом и остаются.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
; Обновление из программы: она запускает установщик с /SILENT /RELAUNCH и
; закрывается. В тихом режиме пункт выше пропускается, поэтому программу
; после установки запускает этот. runasoriginaluser — чтобы при установке
; для всех пользователей программа не осталась с правами администратора.
Filename: "{app}\{#AppExe}"; Flags: nowait runasoriginaluser; Check: RelaunchRequested

[Code]
function RelaunchRequested: Boolean;
var
  I: Integer;
begin
  Result := False;
  for I := 1 to ParamCount do
    if CompareText(ParamStr(I), '/RELAUNCH') = 0 then
    begin
      Result := True;
      Exit;
    end;
end;

// «Запускать вместе с Windows» из окна программы пишет её путь в автозапуск;
// после удаления программы он вёл бы в никуда.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    RegDeleteValue(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Run', '{#AppName}');
end;
