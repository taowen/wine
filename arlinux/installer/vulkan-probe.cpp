// SPDX-License-Identifier: MIT
#include <vulkan/vulkan.h>
#include <dlfcn.h>
#include <cstdio>
#include <cstring>
#include <stdexcept>
#include <vector>
#include "dxvk_device_requirements.h"

namespace {
void check(VkResult result) { if (result != VK_SUCCESS) throw std::runtime_error("Vulkan capability query failed"); }
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
        if (argc != 2) throw std::runtime_error("Expected the selected runtime Vulkan loader");
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
            bool graphics = false;
            for (const auto& queue : queues) graphics |= queue.queueCount && (queue.queueFlags & VK_QUEUE_GRAPHICS_BIT);
            if (!graphics) continue;
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
        std::printf("{\"modern\":%s,\"legacy\":%s}\n", modern ? "true" : "false", legacy ? "true" : "false");
        return 0;
    } catch (const std::exception& error) {
        std::fprintf(stderr, "Wine Vulkan capability probe: %s\n", error.what());
        return 1;
    }
}
