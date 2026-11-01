// SPDX-License-Identifier: MIT
#include <vulkan/vulkan.h>
#include <dlfcn.h>
#include <cstdio>
#include <cstring>
#include <cstdlib>
#include <filesystem>
#include <stdexcept>
#include <vector>
#include <memory>
#include <unistd.h>
#include "dxvk_device_requirements.h"

namespace {
void check(VkResult result) { if (result != VK_SUCCESS) throw std::runtime_error("Vulkan capability query failed: VkResult " + std::to_string(result)); }
struct Features {
    VkPhysicalDeviceFeatures2 core{VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_FEATURES_2};
    VkPhysicalDeviceVulkan11Features vk11{VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_1_1_FEATURES};
    VkPhysicalDeviceVulkan12Features vk12{VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_1_2_FEATURES};
    VkPhysicalDeviceVulkan13Features vk13{VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_1_3_FEATURES};
    VkPhysicalDeviceRobustness2FeaturesEXT extRobustness2{VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_ROBUSTNESS_2_FEATURES_EXT};
    VkPhysicalDeviceMaintenance5FeaturesKHR khrMaintenance5{VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_MAINTENANCE_5_FEATURES_KHR};
};
}
int main(int argc, char** argv) {
    try {
        // Vendor drivers may write diagnostics directly to stdout. Keep the
        // protocol stream private before loading them; diagnostics use stderr.
        const int output = dup(STDOUT_FILENO);
        if (output < 0) throw std::runtime_error("Cannot duplicate probe output");
        std::unique_ptr<FILE, decltype(&std::fclose)> result(fdopen(output, "w"), &std::fclose);
        if (!result) { close(output); throw std::runtime_error("Cannot open probe output"); }
        if (dup2(STDERR_FILENO, STDOUT_FILENO) < 0)
            throw std::runtime_error("Cannot redirect driver diagnostics");
        if (argc != 3) throw std::runtime_error("Expected runtime Vulkan loader and driver (turnip or system)");
        const std::string driver = argv[2];
        if (driver != "turnip" && driver != "system") throw std::runtime_error("Unknown Vulkan driver");
        auto files = std::filesystem::path(argv[1]).parent_path().parent_path().parent_path().parent_path();
        auto manifest = files / "share/vulkan/icd.d" / (driver + "_icd.aarch64.json");
        if (!std::filesystem::is_regular_file(manifest)) throw std::runtime_error("Driver manifest missing: " + driver);
        // Each candidate runs in its own process. Never reuse a loader which
        // has already initialized or rejected another ICD.
        for (auto name : {"VK_ICD_FILENAMES", "VK_DRIVER_FILES", "VK_INSTANCE_LAYERS", "VK_LAYER_PATH",
                          "VK_ADD_LAYER_PATH", "VK_ADD_DRIVER_FILES", "VK_LOADER_LAYERS_ENABLE"}) unsetenv(name);
        setenv("VK_DRIVER_FILES", manifest.c_str(), 1);
        setenv("VK_LOADER_LAYERS_DISABLE", "~implicit~", 1);
        if (driver == "system") {
            auto layers = files / "share/vulkan/explicit_layer.d";
            setenv("VK_LAYER_PATH", layers.c_str(), 1);
            setenv("VK_INSTANCE_LAYERS", "VK_LAYER_HYBRIS_compat", 1);
            setenv("HYBRIS_BC_TEXTURES", "missing", 1);
        }
        void* library = dlopen(argv[1], RTLD_NOW | RTLD_LOCAL);
        if (!library) throw std::runtime_error(dlerror());
        auto get = reinterpret_cast<PFN_vkGetInstanceProcAddr>(dlsym(library, "vkGetInstanceProcAddr"));
        if (!get) throw std::runtime_error("Vulkan loader entry missing");
        auto create = reinterpret_cast<PFN_vkCreateInstance>(get(nullptr, "vkCreateInstance"));
        auto enumerateVersion = reinterpret_cast<PFN_vkEnumerateInstanceVersion>(get(nullptr, "vkEnumerateInstanceVersion"));
        uint32_t loaderVersion = VK_API_VERSION_1_0;
        if (enumerateVersion) check(enumerateVersion(&loaderVersion));
        VkApplicationInfo application{VK_STRUCTURE_TYPE_APPLICATION_INFO};
        application.pApplicationName = "wine-installer";
        application.apiVersion = loaderVersion >= VK_API_VERSION_1_3 ? VK_API_VERSION_1_3 : VK_API_VERSION_1_1;
        VkInstanceCreateInfo info{VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO};
        info.pApplicationInfo = &application;
        VkInstance instance = VK_NULL_HANDLE;
        check(create(&info, nullptr, &instance));
#define FUNCTION(name) auto name = reinterpret_cast<PFN_##name>(get(instance, #name)); if (!name) throw std::runtime_error("Vulkan instance entry missing: " #name)
        FUNCTION(vkDestroyInstance);
        FUNCTION(vkEnumeratePhysicalDevices);
        FUNCTION(vkGetPhysicalDeviceProperties);
        FUNCTION(vkGetPhysicalDeviceFeatures2);
        FUNCTION(vkGetPhysicalDeviceQueueFamilyProperties);
        FUNCTION(vkEnumerateDeviceExtensionProperties);
        FUNCTION(vkCreateDevice);
        FUNCTION(vkDestroyDevice);
#undef FUNCTION
        uint32_t count = 0;
        check(vkEnumeratePhysicalDevices(instance, &count, nullptr));
        std::vector<VkPhysicalDevice> devices(count);
        check(vkEnumeratePhysicalDevices(instance, &count, devices.data()));
        bool modern = false, legacy = false;
        for (VkPhysicalDevice device : devices) {
            VkPhysicalDeviceProperties properties;
            vkGetPhysicalDeviceProperties(device, &properties);
            if (properties.deviceType == VK_PHYSICAL_DEVICE_TYPE_CPU) continue;
            uint32_t queueCount = 0;
            vkGetPhysicalDeviceQueueFamilyProperties(device, &queueCount, nullptr);
            std::vector<VkQueueFamilyProperties> queues(queueCount);
            vkGetPhysicalDeviceQueueFamilyProperties(device, &queueCount, queues.data());
            uint32_t graphics = queueCount;
            for (uint32_t queue = 0; queue < queueCount; ++queue)
                if (queues[queue].queueCount && (queues[queue].queueFlags & VK_QUEUE_GRAPHICS_BIT)) { graphics = queue; break; }
            if (graphics == queueCount) continue;
            float priority = 1.0f;
            VkDeviceQueueCreateInfo queueInfo{VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO};
            queueInfo.queueFamilyIndex = graphics;
            queueInfo.queueCount = 1;
            queueInfo.pQueuePriorities = &priority;
            VkDeviceCreateInfo deviceInfo{VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO};
            deviceInfo.queueCreateInfoCount = 1;
            deviceInfo.pQueueCreateInfos = &queueInfo;
            VkDevice logical;
            check(vkCreateDevice(device, &deviceInfo, nullptr, &logical));
            vkDestroyDevice(logical, nullptr);
            legacy |= properties.apiVersion >= VK_API_VERSION_1_1;
            if (properties.apiVersion < dxvk::DxvkRequiredVulkanApiVersion ||
                properties.limits.maxPushConstantsSize < dxvk::DxvkRequiredPushConstantSize) continue;
            uint32_t extensionCount = 0;
            check(vkEnumerateDeviceExtensionProperties(device, nullptr, &extensionCount, nullptr));
            std::vector<VkExtensionProperties> extensions(extensionCount);
            check(vkEnumerateDeviceExtensionProperties(device, nullptr, &extensionCount, extensions.data()));
            auto has = [&](const char* name) {
                for (const auto& extension : extensions) if (!std::strcmp(name, extension.extensionName)) return true;
                return false;
            };
            Features features;
            features.core.pNext = &features.vk11;
            features.vk11.pNext = &features.vk12;
            features.vk12.pNext = &features.vk13;
            void** next = &features.vk13.pNext;
            if (has(VK_EXT_ROBUSTNESS_2_EXTENSION_NAME)) { *next = &features.extRobustness2; next = &features.extRobustness2.pNext; }
            if (has(VK_KHR_MAINTENANCE_5_EXTENSION_NAME)) { *next = &features.khrMaintenance5; next = &features.khrMaintenance5.pNext; }
            *next = nullptr;
            vkGetPhysicalDeviceFeatures2(device, &features.core);
            struct { bool khrSwapchain; } supported{has(VK_KHR_SWAPCHAIN_EXTENSION_NAME)};
            // The exact feature declarations are shared with DXVK, not copied into APK policy.
#define REQUIRED_true(value, name) ((value) ? nullptr : name)
#define REQUIRED_false(value, name) nullptr
#define ENABLE_FEATURE(version, name, required) REQUIRED_##required(features.version.name, #name)
#define ENABLE_EXT_FEATURE(extension, name, required) REQUIRED_##required(features.extension.name, #name)
#define ENABLE_EXT(extension, required) REQUIRED_##required(supported.extension, #extension)
            const char* missing[] = {
#include "dxvk_device_features.inc"
            };
#undef ENABLE_FEATURE
#undef ENABLE_EXT_FEATURE
#undef ENABLE_EXT
#undef REQUIRED_true
#undef REQUIRED_false
            bool admitted = true;
            for (const char* name : missing) if (name) { std::fprintf(stderr, "DXVK requirement unavailable: %s\n", name); admitted = false; }
            modern |= admitted;
        }
        vkDestroyInstance(instance, nullptr);
        dlclose(library);
        if (std::fprintf(result.get(), "{\"modern\":%s,\"legacy\":%s}\n", modern ? "true" : "false", legacy ? "true" : "false") < 0 ||
            std::fflush(result.get()) != 0) throw std::runtime_error("Cannot write probe output");
        return 0;
    } catch (const std::exception& error) {
        std::fprintf(stderr, "Wine Vulkan capability probe: %s\n", error.what());
        return 1;
    }
}
