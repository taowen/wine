// SPDX-License-Identifier: MIT
#include <windows.h>
#include <cstdio>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace {
constexpr DWORD capacity = 32768;
[[noreturn]] void fail(const char* operation, DWORD error = GetLastError()) {
    throw std::runtime_error(std::string(operation) + ": Win32 error " + std::to_string(error));
}
struct Key {
    HKEY handle = nullptr;
    Key(HKEY parent, const wchar_t* path) {
        LSTATUS result = RegCreateKeyExW(parent, path, 0, nullptr, 0, KEY_SET_VALUE, nullptr, &handle, nullptr);
        if (result) fail("Open runtime registry key", result);
    }
    ~Key() { RegCloseKey(handle); }
    void text(const std::wstring& name, const std::wstring& value) {
        LSTATUS result = RegSetValueExW(handle, name.c_str(), 0, REG_SZ,
            reinterpret_cast<const BYTE*>(value.c_str()), (value.size() + 1) * sizeof(wchar_t));
        if (result) fail("Write runtime registry value", result);
    }
};
bool exists(const std::wstring& path) { return GetFileAttributesW(path.c_str()) != INVALID_FILE_ATTRIBUTES; }
std::vector<std::wstring> files(const std::wstring& directory, const wchar_t* pattern) {
    WIN32_FIND_DATAW data;
    HANDLE search = FindFirstFileW((directory + L"\\" + pattern).c_str(), &data);
    if (search == INVALID_HANDLE_VALUE) {
        if (GetLastError() == ERROR_FILE_NOT_FOUND) return {};
        fail("Enumerate runtime files");
    }
    std::vector<std::wstring> result;
    do { if (!(data.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY)) result.emplace_back(data.cFileName); }
    while (FindNextFileW(search, &data));
    DWORD error = GetLastError();
    FindClose(search);
    if (error != ERROR_NO_MORE_FILES) fail("Enumerate runtime files", error);
    return result;
}
void copy(const std::wstring& source, const std::wstring& destination) {
    if (!CopyFileW(source.c_str(), destination.c_str(), FALSE)) fail("Copy runtime file");
}
void wineboot() {
    wchar_t command[] = L"wineboot.exe -u";
    STARTUPINFOW startup = {}; startup.cb = sizeof(startup);
    PROCESS_INFORMATION process = {};
    if (!CreateProcessW(nullptr, command, nullptr, nullptr, TRUE, 0, nullptr, nullptr, &startup, &process)) fail("Start wineboot");
    CloseHandle(process.hThread);
    DWORD status = WaitForSingleObject(process.hProcess, 120000), code = 1;
    BOOL completed = status == WAIT_OBJECT_0 && GetExitCodeProcess(process.hProcess, &code);
    if (!completed) { TerminateProcess(process.hProcess, 1); WaitForSingleObject(process.hProcess, 5000); }
    CloseHandle(process.hProcess);
    if (!completed || code) throw std::runtime_error("Wine prefix initialization failed or timed out");
}
void setup(const std::wstring& root) {
    std::wstring windows(capacity, L'\0');
    UINT length = GetWindowsDirectoryW(windows.data(), windows.size());
    if (!length || length >= windows.size()) fail("Resolve Windows directory");
    windows.resize(length);
    auto fonts = windows + L"\\Fonts";
    if (!CreateDirectoryW(fonts.c_str(), nullptr) && GetLastError() != ERROR_ALREADY_EXISTS) fail("Create font directory");
    auto sourceFonts = root + L"\\share\\arlinux\\fonts";
    for (auto suffix : {L"*.ttf", L"*.ttc", L"*.otf"})
        for (const auto& name : files(sourceFonts, suffix)) copy(sourceFonts + L"\\" + name, fonts + L"\\" + name);
    const std::wstring cjk = L"NotoSansCJK-Regular.ttc";
    bool systemCjk = exists(L"Z:\\system\\fonts\\" + cjk);
    if (systemCjk) copy(L"Z:\\system\\fonts\\" + cjk, fonts + L"\\" + cjk);
    wineboot();
    Key installed(HKEY_LOCAL_MACHINE, L"Software\\Microsoft\\Windows NT\\CurrentVersion\\Fonts");
    for (auto entry : {std::pair{L"Arial", L"arial.ttf"}, std::pair{L"Arial Bold", L"arialbd.ttf"},
            std::pair{L"Arial Italic", L"ariali.ttf"}, std::pair{L"Arial Bold Italic", L"arialbi.ttf"}})
        if (exists(fonts + L"\\" + entry.second)) installed.text(std::wstring(entry.first) + L" (TrueType)", entry.second);
    bool yahei = exists(fonts + L"\\msyh.ttc");
    if (yahei) installed.text(L"Microsoft YaHei & Microsoft YaHei UI (TrueType)", L"msyh.ttc");
    if (systemCjk) {
        installed.text(L"Noto Sans CJK (TrueType)", cjk);
        Key substitutes(HKEY_LOCAL_MACHINE, L"Software\\Microsoft\\Windows NT\\CurrentVersion\\FontSubstitutes");
        for (auto family : {L"SimSun", L"NSimSun", L"SimHei", L"宋体", L"新宋体", L"黑体"}) substitutes.text(family, L"Noto Sans CJK SC");
        if (!yahei) for (auto family : {L"Microsoft YaHei", L"Microsoft YaHei UI", L"微软雅黑"}) substitutes.text(family, L"Noto Sans CJK SC");
        Key links(HKEY_LOCAL_MACHINE, L"Software\\Microsoft\\Windows NT\\CurrentVersion\\FontLink\\SystemLink");
        std::wstring link = cjk + L",Noto Sans CJK SC"; link.push_back(0); link.push_back(0);
        for (auto family : {L"Tahoma", L"Arial", L"Microsoft Sans Serif", L"Segoe UI"}) {
            LSTATUS result = RegSetValueExW(links.handle, family, 0, REG_MULTI_SZ, reinterpret_cast<const BYTE*>(link.data()), link.size()*sizeof(wchar_t));
            if (result) fail("Register font fallback", result);
        }
    }
    // The downloader installs exactly one DXVK producer into the flat runtime directory.
    Key overrides(HKEY_CURRENT_USER, L"Software\\Wine\\DllOverrides");
    const auto receipt = windows + L"\\arlinux-runtime.ini";
    std::wstring previous(capacity, L'\0');
    DWORD count = GetPrivateProfileStringW(L"graphics", L"dlls", L"", previous.data(), previous.size(), receipt.c_str());
    if (count >= previous.size() - 1) throw std::runtime_error("Invalid prior runtime installation receipt");
    previous.resize(count);
    std::vector<std::wstring> old;
    for (size_t offset = 0; offset < previous.size();) {
        size_t end = previous.find(L',', offset);
        auto name = previous.substr(offset, end == previous.npos ? previous.npos : end - offset);
        if (name.size() <= 4 || name.compare(name.size() - 4, 4, L".dll") != 0 ||
            name.substr(0, name.size() - 4).find_first_not_of(L"abcdefghijklmnopqrstuvwxyz0123456789_") != name.npos)
            throw std::runtime_error("Unsafe prior runtime DLL name");
        old.push_back(name);
        if (end == previous.npos) break;
        offset = end + 1;
    }
    std::wstring current;
    for (auto architecture : {std::pair{L"aarch64-windows", L"system32"}, std::pair{L"i386-windows", L"syswow64"}}) {
        auto target = windows + L"\\" + architecture.second;
        if (!CreateDirectoryW(target.c_str(), nullptr) && GetLastError() != ERROR_ALREADY_EXISTS) fail("Create graphics directory");
        for (const auto& name : old) {
            if (!DeleteFileW((target + L"\\" + name).c_str()) && GetLastError() != ERROR_FILE_NOT_FOUND) fail("Remove prior runtime DLL");
            auto base = name.substr(0, name.size() - 4);
            LSTATUS result = RegDeleteValueW(overrides.handle, base.c_str());
            if (result && result != ERROR_FILE_NOT_FOUND) fail("Clear prior runtime DLL policy", result);
        }
        auto source = root + L"\\lib\\wine\\dxvk\\" + architecture.first;
        auto dlls = files(source, L"*.dll");
        if (dlls.empty()) throw std::runtime_error("Selected graphics component contains no DLLs");
        for (const auto& name : dlls) {
            // Remove Wine's builtin symlink before installing an independent native DLL.
            if (!DeleteFileW((target + L"\\" + name).c_str()) && GetLastError() != ERROR_FILE_NOT_FOUND) fail("Replace runtime DLL");
            copy(source + L"\\" + name, target + L"\\" + name);
            overrides.text(name.substr(0, name.size() - 4), L"native,builtin");
            if (architecture.first == std::wstring(L"aarch64-windows")) { if (!current.empty()) current += L','; current += name; }
        }
    }
    if (!WritePrivateProfileStringW(L"graphics", L"dlls", current.c_str(), receipt.c_str())) fail("Write runtime installation receipt");
    std::fprintf(stderr, "Wine runtime prefix installation complete\n");
}
}
int wmain(int argc, wchar_t** argv) {
    try {
        if (argc != 2) throw std::runtime_error("Expected the installed runtime files directory");
        setup(argv[1]);
        return 0;
    } catch (const std::exception& error) {
        std::fprintf(stderr, "Wine runtime installation: %s\n", error.what());
        return 1;
    }
}
