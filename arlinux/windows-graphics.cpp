/* SPDX-License-Identifier: MIT */
#include <windows.h>
#include <dxgi1_2.h>
#include <algorithm>
#include <cstdio>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace {
constexpr DWORD pathCapacity = 32768;

[[noreturn]] void fail(const char* operation, DWORD error = GetLastError()) {
    throw std::runtime_error(std::string(operation) + ": Win32 error " + std::to_string(error));
}

std::wstring property(const std::wstring& ini, const std::wstring& section, const wchar_t* key) {
    std::wstring value(pathCapacity, L'\0');
    DWORD count = GetPrivateProfileStringW(section.c_str(), key, nullptr, value.data(), value.size(), ini.c_str());
    if (!count || count == value.size() - 1) throw std::runtime_error("Invalid graphics profile property");
    value.resize(count);
    return value;
}

bool identifier(const std::wstring& value) {
    return !value.empty() && std::all_of(value.begin(), value.end(), [](wchar_t c) {
        return (c >= L'a' && c <= L'z') || (c >= L'0' && c <= L'9') || c == L'_' || c == L'-';
    });
}

std::vector<std::wstring> names(const std::wstring& text) {
    std::vector<std::wstring> result;
    for (size_t offset = 0;;) {
        size_t end = text.find(L',', offset);
        auto name = text.substr(offset, end == text.npos ? text.npos : end - offset);
        if (!identifier(name)) throw std::runtime_error("Invalid graphics profile identifier");
        result.push_back(name);
        if (end == text.npos) return result;
        offset = end + 1;
    }
}

/* Use each DXVK version's own hardware admission logic, without a second feature table. */
int probe(const wchar_t* dll) {
    const GUID iid = {0x770aae78,0xf26f,0x4dba,{0xa8,0x29,0x25,0x3c,0x83,0xd1,0xb3,0x87}};
    using CreateFactory = HRESULT (WINAPI *)(REFIID, void**);
    HMODULE library = LoadLibraryExW(dll, nullptr, LOAD_WITH_ALTERED_SEARCH_PATH);
    if (!library) fail("Load graphics profile");
    auto create = reinterpret_cast<CreateFactory>(GetProcAddress(library, "CreateDXGIFactory1"));
    if (!create) { FreeLibrary(library); throw std::runtime_error("DXGI factory export missing"); }
    IDXGIFactory1* factory = nullptr;
    HRESULT hr = create(iid, reinterpret_cast<void**>(&factory));
    if (FAILED(hr)) {
        FreeLibrary(library);
        throw std::runtime_error("DXGI factory failed, HRESULT " + std::to_string(static_cast<unsigned long>(hr)));
    }
    int status = 2;
    for (UINT index = 0;; ++index) {
        IDXGIAdapter1* adapter = nullptr;
        hr = factory->EnumAdapters1(index, &adapter);
        if (hr == DXGI_ERROR_NOT_FOUND) break;
        if (FAILED(hr)) { status = 1; break; }
        DXGI_ADAPTER_DESC1 description;
        hr = adapter->GetDesc1(&description);
        adapter->Release();
        if (FAILED(hr)) { status = 1; break; }
        if (!(description.Flags & DXGI_ADAPTER_FLAG_SOFTWARE)) {
            std::fprintf(stderr, "DXVK: hardware admitted, vendor=0x%04x device=0x%04x\n",
                description.VendorId, description.DeviceId);
            status = 0;
            break;
        }
    }
    factory->Release();
    FreeLibrary(library);
    return status;
}

int probeChild(const std::wstring& exe, const std::wstring& dll) {
    std::wstring command = L"\"" + exe + L"\" --probe \"" + dll + L"\"";
    STARTUPINFOW startup = {};
    startup.cb = sizeof(startup);
    startup.dwFlags = STARTF_USESTDHANDLES;
    startup.hStdInput = GetStdHandle(STD_INPUT_HANDLE);
    startup.hStdOutput = GetStdHandle(STD_OUTPUT_HANDLE);
    startup.hStdError = GetStdHandle(STD_ERROR_HANDLE);
    PROCESS_INFORMATION process = {};
    if (!CreateProcessW(exe.c_str(), command.data(), nullptr, nullptr, TRUE, 0, nullptr, nullptr, &startup, &process))
        fail("Start graphics probe");
    CloseHandle(process.hThread);
    DWORD wait = WaitForSingleObject(process.hProcess, 20000);
    DWORD code = 1;
    BOOL read = wait == WAIT_OBJECT_0 && GetExitCodeProcess(process.hProcess, &code);
    DWORD error = GetLastError();
    if (!read) {
        TerminateProcess(process.hProcess, 1);
        WaitForSingleObject(process.hProcess, 5000);
    }
    CloseHandle(process.hProcess);
    if (wait == WAIT_TIMEOUT) throw std::runtime_error("Graphics probe timed out");
    if (!read) fail("Wait for graphics probe", error);
    if (code != 0 && code != 2) throw std::runtime_error("Graphics probe failed, exit " + std::to_string(code));
    return code;
}

struct Profile {
    std::wstring name, version, directory;
    std::vector<std::wstring> dlls;
};

void setup(const std::wstring& exe) {
    const auto root = exe.substr(0, exe.find_last_of(L'\\')) + L"\\..\\wine\\dxvk";
    const auto ini = root + L"\\profiles.ini";
    if (GetPrivateProfileIntW(L"runtime", L"format", 0, ini.c_str()) != 1)
        throw std::runtime_error("Unsupported graphics profile format");
    std::vector<Profile> profiles;
    for (const auto& name : names(property(ini, L"runtime", L"profiles"))) {
        Profile profile;
        profile.name = name;
        profile.version = property(ini, name, L"version");
        auto directory = property(ini, name, L"directory");
        if (directory != L"." && !identifier(directory)) throw std::runtime_error("Unsafe graphics profile directory");
        profile.directory = root + L"\\" + directory;
        profile.dlls = names(property(ini, name, L"dlls"));
        profiles.push_back(profile);
    }
    // A probe must load the requested native DLL even if the caller is diagnosing builtin DXGI.
    std::wstring overrides(pathCapacity, L'\0');
    DWORD length = GetEnvironmentVariableW(L"WINEDLLOVERRIDES", overrides.data(), overrides.size());
    if (length >= overrides.size()) throw std::runtime_error("DLL override environment too long");
    overrides.resize(length);
    overrides += L";dxgi=n";
    if (!SetEnvironmentVariableW(L"WINEDLLOVERRIDES", overrides.c_str())) fail("Set probe DLL policy");
    const Profile* selected = nullptr;
    for (const auto& profile : profiles) {
        // Separate processes prevent one profile's DXGI module from leaking into another probe.
        if (probeChild(exe, profile.directory + L"\\aarch64-windows\\dxgi.dll") == 0) {
            selected = &profile;
            break;
        }
    }
    if (!selected) throw std::runtime_error("No graphics profile admits this device's hardware Vulkan driver");
    std::wstring windows(pathCapacity, L'\0');
    length = GetWindowsDirectoryW(windows.data(), windows.size());
    if (!length || length >= windows.size()) fail("Resolve Windows directory");
    windows.resize(length);
    for (const auto& pair : {std::pair{L"aarch64-windows", L"system32"}, std::pair{L"i386-windows", L"syswow64"}}) {
        auto target = windows + L"\\" + pair.second;
        if (!CreateDirectoryW(target.c_str(), nullptr) && GetLastError() != ERROR_ALREADY_EXISTS) fail("Create graphics directory");
        for (const auto& profile : profiles) for (const auto& dll : profile.dlls) {
            auto path = target + L"\\" + dll + L".dll";
            if (!DeleteFileW(path.c_str()) && GetLastError() != ERROR_FILE_NOT_FOUND) fail("Remove previous graphics DLL");
        }
        for (const auto& dll : selected->dlls) {
            auto source = selected->directory + L"\\" + pair.first + L"\\" + dll + L".dll";
            auto destination = target + L"\\" + dll + L".dll";
            if (!CopyFileW(source.c_str(), destination.c_str(), FALSE)) fail("Install graphics DLL");
        }
    }
    struct Key { HKEY handle = nullptr; ~Key() { if (handle) RegCloseKey(handle); } } key;
    LSTATUS error = RegCreateKeyExW(HKEY_CURRENT_USER, L"Software\\Wine\\DllOverrides", 0, nullptr, 0,
        KEY_SET_VALUE, nullptr, &key.handle, nullptr);
    if (error) fail("Open graphics DLL policy", error);
    for (const auto& profile : profiles) for (const auto& dll : profile.dlls) {
        error = RegDeleteValueW(key.handle, dll.c_str());
        if (error && error != ERROR_FILE_NOT_FOUND) fail("Clear previous graphics DLL policy", error);
    }
    constexpr wchar_t policy[] = L"native,builtin";
    for (const auto& dll : selected->dlls) {
        error = RegSetValueExW(key.handle, dll.c_str(), 0, REG_SZ, reinterpret_cast<const BYTE*>(policy), sizeof(policy));
        if (error) fail("Set graphics DLL policy", error);
    }
    std::fprintf(stderr, "DXVK selected: %s %s\n", std::string(selected->name.begin(), selected->name.end()).c_str(),
        std::string(selected->version.begin(), selected->version.end()).c_str());
    auto receipt = windows + L"\\arlinux-graphics.ini";
    if (!WritePrivateProfileStringW(L"graphics", L"profile", selected->name.c_str(), receipt.c_str()) ||
        !WritePrivateProfileStringW(L"graphics", L"version", selected->version.c_str(), receipt.c_str()))
        fail("Write graphics selection receipt");
}
}

int wmain(int argc, wchar_t** argv) {
    try {
        if (argc == 3 && std::wstring(argv[1]) == L"--probe") return probe(argv[2]);
        if (argc != 1) throw std::runtime_error("Unexpected graphics setup arguments");
        std::wstring exe(pathCapacity, L'\0');
        DWORD length = GetModuleFileNameW(nullptr, exe.data(), exe.size());
        if (!length || length >= exe.size()) fail("Resolve graphics setup executable");
        exe.resize(length);
        setup(exe);
        return 0;
    } catch (const std::exception& error) {
        std::fprintf(stderr, "Windows graphics initialization: %s\n", error.what());
        return 1;
    }
}
