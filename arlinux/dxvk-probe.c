/* SPDX-License-Identifier: MIT */
#define CINTERFACE
#define COBJMACROS
#include <windows.h>
#include <dxgi1_2.h>
#include <stdio.h>

/* Ask the selected DXVK build itself which hardware adapters it admits. */
int wmain(int argc, wchar_t **argv)
{
    static const GUID factory_iid = {0x770aae78,0xf26f,0x4dba,{0xa8,0x29,0x25,0x3c,0x83,0xd1,0xb3,0x87}};
    typedef HRESULT (WINAPI *create_factory)(REFIID, void **);
    if (argc != 2) return 1;
    HMODULE library = LoadLibraryExW(argv[1], NULL, LOAD_WITH_ALTERED_SEARCH_PATH);
    if (!library) {
        fprintf(stderr, "DXVK probe: DLL load failed, Win32 error %lu\n", GetLastError());
        return 1;
    }
    create_factory create = (create_factory)GetProcAddress(library, "CreateDXGIFactory1");
    if (!create) { fprintf(stderr, "DXVK probe: factory export missing\n"); FreeLibrary(library); return 1; }
    IDXGIFactory1 *factory = NULL;
    HRESULT result = create(&factory_iid, (void **)&factory);
    if (FAILED(result)) {
        fprintf(stderr, "DXVK probe: factory failed, HRESULT 0x%08lx\n", (unsigned long)result);
        FreeLibrary(library);
        return 1;
    }
    int status = 2;
    for (UINT index = 0;; ++index) {
        IDXGIAdapter1 *adapter = NULL;
        result = IDXGIFactory1_EnumAdapters1(factory, index, &adapter);
        if (result == DXGI_ERROR_NOT_FOUND) break;
        if (FAILED(result)) { status = 1; break; }
        DXGI_ADAPTER_DESC1 description;
        result = IDXGIAdapter1_GetDesc1(adapter, &description);
        IDXGIAdapter1_Release(adapter);
        if (FAILED(result)) { status = 1; break; }
        if (!(description.Flags & DXGI_ADAPTER_FLAG_SOFTWARE)) {
            fprintf(stderr, "DXVK probe: hardware adapter admitted, vendor=0x%04x device=0x%04x\n",
                    description.VendorId, description.DeviceId);
            status = 0;
            break;
        }
    }
    IDXGIFactory1_Release(factory);
    FreeLibrary(library);
    if (status == 2) fprintf(stderr, "DXVK probe: no compatible hardware adapter\n");
    return status;
}
