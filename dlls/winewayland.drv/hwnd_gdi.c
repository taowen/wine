/*
 * Wayland window surface implementation
 *
 * Copyright 2020 Alexandros Frantzis for Collabora Ltd
 *
 * This library is free software; you can redistribute it and/or
 * modify it under the terms of the GNU Lesser General Public
 * License as published by the Free Software Foundation; either
 * version 2.1 of the License, or (at your option) any later version.
 *
 * This library is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
 * Lesser General Public License for more details.
 *
 * You should have received a copy of the GNU Lesser General Public
 * License along with this library; if not, write to the Free Software
 * Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA 02110-1301, USA
 */

/* Shared-memory consumer adapted from Erhan Bilgili's Wine-Wineland work:
 * nanomatters/wine-wineland 908f7702fbf6e6edb761daa47476d283e52b60f1.
 * Only the foreign GDI transport is enabled; existing GPU WSI is unchanged. */
#if 0
#pragma makedep unix
#endif
#include "config.h"
#include <stdlib.h>
#include <string.h>
#include <limits.h>
#include <time.h>
#include <unistd.h>
#include <sys/socket.h>
#include <errno.h>
#include "waylanddrv.h"
#include "dxgi1_2.h"
#include "wine/hwnd_dmabuf.h"
#include "wine/debug.h"
WINE_DEFAULT_DEBUG_CHANNEL(hwndgdi);

struct wayland_hwnd_dmabuf_surface;

struct wayland_hwnd_dmabuf_buffer
{
    struct wl_list link;
    struct wayland_hwnd_dmabuf_surface *surface;
    struct wl_buffer *wl_buffer;
    UINT64 producer_unique_id;
    UINT64 release_token;
    UINT64 modifier;              /* cached-slot layout identity */
    unsigned int image_id;        /* producer ring slot. Cache key (stable-slot path) */
    unsigned int ring_generation; /* swapchain-rebuild counter. Invalidates a cached slot */
    unsigned int fourcc;
    unsigned int stride;
    unsigned int offset;
    unsigned int alpha_mode;
    int width;
    int height;
    LONG ref;          /* owner ref + one ref per outstanding compositor commit */
    LONG released;     /* uncached path: set by wl_buffer.release, reaped next pass */
    LONG cache_valid;  /* stable-slot cache entry is still retained by this surface */
    BOOL stable_slot;  /* cached and reused per slot. Release token sent on release */
    unsigned int release_flags;
    int channel_fd;    /* dup of surface->channel_fd for sending release tokens */
};

struct wayland_hwnd_dmabuf_surface
{
    struct wl_list link;
    HWND hwnd;
    struct wl_surface *wl_surface;
    struct wl_subsurface *wl_subsurface;
    struct wp_viewport *wp_viewport;
    struct wayland_hwnd_dmabuf_buffer *current;
    struct wl_list buffers;
    UINT frame_seq;
    BOOL seen;
    unsigned long long last_seen_ms; /* tick when last present in the child list */
    int channel_fd;                 /* consumer end of the producer socket, or -1 */
};

/* A child may briefly drop out of the descendant list between frames. Tearing its surface
 * (and dmabuf cache) down on a single miss churns the cache and, with fd-once, strands slots
 * the producer thinks are still cached. Keep an unseen surface for a grace window first. */
#define WAYLAND_DMABUF_SURFACE_GRACE_MS 1000

static unsigned long long wayland_time_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (unsigned long long)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

static unsigned int wayland_hwnd_dmabuf_buffer_cache_flags(struct wayland_hwnd_dmabuf_buffer *buffer)
{
    if (buffer->stable_slot && InterlockedCompareExchange(&buffer->cache_valid, FALSE, FALSE))
        return HWND_DMABUF_RELEASE_CACHED;
    return 0;
}

static UINT64 wayland_hwnd_dmabuf_buffer_exchange_release_token(struct wayland_hwnd_dmabuf_buffer *buffer,
                                                                UINT64 release_token)
{
    LONGLONG volatile *token = (LONGLONG volatile *)&buffer->release_token;
    LONGLONG old;

    do old = *token;
    while (InterlockedCompareExchange64(token, release_token, old) != old);
    return old;
}

/* Send a release record, retrying EINTR. */
static int wayland_hwnd_dmabuf_channel_send_release(int channel_fd, const hwnd_dmabuf_release_t *rel)
{
    ssize_t n;

    do n = send(channel_fd, rel, sizeof(*rel), MSG_DONTWAIT | MSG_NOSIGNAL);
    while (n < 0 && errno == EINTR);
    return n == sizeof(*rel) ? 0 : n < 0 ? errno : EMSGSIZE;
}

static void wayland_hwnd_dmabuf_buffer_send_release(struct wayland_hwnd_dmabuf_buffer *buffer,
                                                    unsigned int flags, BOOL keep_token)
{
    UINT64 release_token;

    if (buffer->channel_fd < 0) return;
    release_token = wayland_hwnd_dmabuf_buffer_exchange_release_token(buffer, 0);
    if (release_token)
    {
        hwnd_dmabuf_release_t rel = { buffer->producer_unique_id, release_token,
                                      flags | wayland_hwnd_dmabuf_buffer_cache_flags(buffer),
                                      buffer->image_id, buffer->ring_generation, 0 };
        if (keep_token && wayland_hwnd_dmabuf_channel_send_release(buffer->channel_fd, &rel))
            InterlockedCompareExchange64((LONGLONG volatile *)&buffer->release_token, release_token, 0);
        else if (!keep_token)
            wayland_hwnd_dmabuf_channel_send_release(buffer->channel_fd, &rel);
    }
}

/* Drop a buffer reference. The last unref destroys the wl_buffer and frees.
 * Lock-free, may run on the present thread or the event thread (inside the
 * wl_buffer.release handler). wl_buffer_destroy never races a release dispatch. */
static void wayland_hwnd_dmabuf_buffer_unref(struct wayland_hwnd_dmabuf_buffer *buffer)
{
    if (InterlockedDecrement(&buffer->ref) > 0) return;
    wayland_hwnd_dmabuf_buffer_send_release(buffer, buffer->release_flags ?
                                            buffer->release_flags : HWND_DMABUF_RELEASE_ORPHANED, FALSE);
    if (buffer->channel_fd >= 0) close(buffer->channel_fd);
    if (buffer->wl_buffer) wl_buffer_destroy(buffer->wl_buffer);
    free(buffer);
}

/* Return a release token to the producer. */
static void wayland_hwnd_dmabuf_send_release(struct wayland_hwnd_dmabuf_surface *surface,
                                             UINT64 producer_unique_id, UINT64 release_token,
                                             unsigned int flags, unsigned int image_id,
                                             unsigned int ring_generation)
{
    hwnd_dmabuf_release_t rel = { producer_unique_id, release_token, flags, image_id, ring_generation, 0 };

    /* One-shot reject/orphan releases are best effort. */
    if (release_token && surface->channel_fd >= 0)
        wayland_hwnd_dmabuf_channel_send_release(surface->channel_fd, &rel);
}

/* Present-thread teardown: detach from the surface and drop the owner ref.
 * Must be called with win_data_mutex held.
 * If the compositor still holds the buffer it survives as an orphan until its
 * release handler drops the last ref. */
static void wayland_hwnd_dmabuf_buffer_reap(struct wayland_hwnd_dmabuf_buffer *buffer)
{
    struct wayland_hwnd_dmabuf_surface *surface = buffer->surface;

    if (surface && surface->current == buffer) surface->current = NULL;
    InterlockedExchange(&buffer->cache_valid, FALSE);
    wl_list_remove(&buffer->link);
    buffer->surface = NULL;
    wayland_hwnd_dmabuf_buffer_unref(buffer);
}

/* wl_buffer.release runs on the event thread: lock-free only, no surface/list
 * mutation. Cached stable-slot buffers stay cached for the slot's next frame.
 * Send the release token now to recycle the slot. The busy gate blocks the
 * producer from overwriting it until then. Uncached buffers are flagged for the
 * present thread to reap next pass (token sent on destroy). Either way drop the
 * per-commit ref. */
static void wayland_hwnd_dmabuf_buffer_handle_release(void *data, struct wl_buffer *wl_buffer)
{
    struct wayland_hwnd_dmabuf_buffer *buffer = data;

    if (buffer->stable_slot)
        wayland_hwnd_dmabuf_buffer_send_release(buffer, HWND_DMABUF_RELEASE_PRESENTED, TRUE);
    else
    {
        buffer->release_flags = HWND_DMABUF_RELEASE_PRESENTED;
        InterlockedExchange(&buffer->released, TRUE);
    }
    wayland_hwnd_dmabuf_buffer_unref(buffer);
}

static const struct wl_buffer_listener wayland_hwnd_dmabuf_buffer_listener =
{
    wayland_hwnd_dmabuf_buffer_handle_release
};

static void wayland_hwnd_dmabuf_surface_destroy(struct wayland_hwnd_dmabuf_surface *surface)
{
    struct wayland_hwnd_dmabuf_buffer *buffer, *buffer_next;

    wl_list_for_each_safe(buffer, buffer_next, &surface->buffers, link)
        wayland_hwnd_dmabuf_buffer_reap(buffer);

    wl_list_remove(&surface->link);
    if (surface->wl_subsurface) wl_subsurface_destroy(surface->wl_subsurface);
    if (surface->wp_viewport) wp_viewport_destroy(surface->wp_viewport);
    if (surface->wl_surface) wl_surface_destroy(surface->wl_surface);
    if (surface->channel_fd >= 0) close(surface->channel_fd);
    free(surface);
}

static void wayland_surface_coords_from_window(struct wayland_surface *surface,
        int x, int y, int *sx, int *sy)
{
    POINT point = {x, y};
    point = map_point_to_surface(surface, point);
    *sx = point.x;
    *sy = point.y;
}

static struct wayland_hwnd_dmabuf_surface *wayland_hwnd_dmabuf_surface_get(struct wayland_surface *parent,
                                                                           HWND hwnd)
{
    struct wayland_hwnd_dmabuf_surface *surface;

    wl_list_for_each(surface, &parent->hwnd_dmabuf_surfaces, link)
        if (surface->hwnd == hwnd) return surface;
    return NULL;
}

static struct wayland_hwnd_dmabuf_surface *wayland_hwnd_dmabuf_surface_create(struct wayland_surface *parent,
                                                                              HWND hwnd)
{
    struct wayland_hwnd_dmabuf_surface *surface;
    struct wl_region *empty_region;

    if (!(surface = calloc(1, sizeof(*surface)))) return NULL;
    surface->hwnd = hwnd;
    surface->channel_fd = -1;
    wl_list_init(&surface->buffers);

    if (!(surface->wl_surface = wl_compositor_create_surface(process_wayland.wl_compositor))) goto err;
    wl_surface_set_user_data(surface->wl_surface, hwnd);

    empty_region = wl_compositor_create_region(process_wayland.wl_compositor);
    if (!empty_region) goto err;
    wl_surface_set_input_region(surface->wl_surface, empty_region);
    wl_region_destroy(empty_region);

    if (!(surface->wp_viewport = wp_viewporter_get_viewport(process_wayland.wp_viewporter,
                                                            surface->wl_surface))) goto err;

    if (!(surface->wl_subsurface = wl_subcompositor_get_subsurface(process_wayland.wl_subcompositor,
                                                                   surface->wl_surface,
                                                                   parent->wl_surface))) goto err;
    wl_subsurface_set_desync(surface->wl_subsurface);

    wl_list_insert(parent->hwnd_dmabuf_surfaces.prev, &surface->link);
    return surface;

err:
    if (surface->wl_subsurface) wl_subsurface_destroy(surface->wl_subsurface);
    if (surface->wp_viewport) wp_viewport_destroy(surface->wp_viewport);
    if (surface->wl_surface) wl_surface_destroy(surface->wl_surface);
    free(surface);
    return NULL;
}

static void wayland_hwnd_dmabuf_surface_set_opaque(struct wayland_hwnd_dmabuf_surface *surface,
                                                   int width, int height)
{
    struct wl_region *region = NULL;

    if (surface->current && surface->current->alpha_mode == DXGI_ALPHA_MODE_IGNORE &&
        (region = wl_compositor_create_region(process_wayland.wl_compositor)))
    {
        wl_region_add(region, 0, 0, width, height);
        wl_surface_set_opaque_region(surface->wl_surface, region);
        wl_region_destroy(region);
    }
    else wl_surface_set_opaque_region(surface->wl_surface, NULL);
}

static BOOL wayland_hwnd_dmabuf_surface_configure(struct wayland_surface *parent,
                                                  struct wayland_hwnd_dmabuf_surface *surface,
                                                  const hwnd_dmabuf_frame_info_t *info,
                                                  struct wl_surface *above)
{
    struct wayland_win_data *parent_data = wayland_win_data_get_nolock(parent->hwnd);
    RECT rect = wine_server_get_rect(info->client), clipped, client;
    double source_x, source_y, source_width, source_height;
    int rect_width, rect_height;
    int x, y, width, height;

    if (!parent_data) return FALSE;
    rect_width = rect.right - rect.left;
    rect_height = rect.bottom - rect.top;
    if (rect_width <= 0 || rect_height <= 0) return FALSE;

    client.left = client.top = 0;
    client.right = parent_data->rects.client.right - parent_data->rects.client.left;
    client.bottom = parent_data->rects.client.bottom - parent_data->rects.client.top;

    clipped.left = max(rect.left, client.left);
    clipped.top = max(rect.top, client.top);
    clipped.right = min(rect.right, client.right);
    clipped.bottom = min(rect.bottom, client.bottom);
    if (IsRectEmpty(&clipped)) return FALSE;

    source_x = (double)(clipped.left - rect.left) * surface->current->width / rect_width;
    source_y = (double)(clipped.top - rect.top) * surface->current->height / rect_height;
    source_width = (double)(clipped.right - clipped.left) * surface->current->width / rect_width;
    source_height = (double)(clipped.bottom - clipped.top) * surface->current->height / rect_height;

    rect = clipped;
    OffsetRect(&rect, parent_data->rects.client.left - parent->window.rect.left,
               parent_data->rects.client.top - parent->window.rect.top);

    wayland_surface_coords_from_window(parent, rect.left, rect.top, &x, &y);
    wayland_surface_coords_from_window(parent, rect.right - rect.left,
                                       rect.bottom - rect.top, &width, &height);
    width = max(1, width);
    height = max(1, height);

    wl_subsurface_set_position(surface->wl_subsurface, x, y);
    wl_subsurface_place_above(surface->wl_subsurface, above);
    wp_viewport_set_source(surface->wp_viewport,
                           wl_fixed_from_double(source_x), wl_fixed_from_double(source_y),
                           wl_fixed_from_double(source_width), wl_fixed_from_double(source_height));
    wp_viewport_set_destination(surface->wp_viewport, width, height);
    wayland_hwnd_dmabuf_surface_set_opaque(surface, width, height);
    return TRUE;
}

/* Claim this child's consumer channel end once. The producer mints it lazily. Retry. */
static void wayland_hwnd_dmabuf_surface_claim_channel(struct wayland_hwnd_dmabuf_surface *surface)
{
    HANDLE handle = 0;
    int fd = -1;

    if (surface->channel_fd >= 0) return;
    if (wine_hwnd_dmabuf_claim_channel(surface->hwnd, &handle) != HWND_DMABUF_OK || !handle)
        return;
    if (wine_server_handle_to_fd(handle, FILE_READ_DATA | FILE_WRITE_DATA, &fd, NULL))
        fd = -1;
    NtClose(handle);
    surface->channel_fd = fd;
    if (fd >= 0) TRACE("hwnd=%p claimed dmabuf socket channel fd %d\n", surface->hwnd, fd);
}

/* Receive one frame. Returns 1 on success, 0 if empty, -1 on EOF. */
static int wayland_hwnd_dmabuf_channel_recv_one(int channel_fd, hwnd_dmabuf_frame_desc_t *desc,
                                                int *out_fd)
{
    char control[CMSG_SPACE(sizeof(int))];
    struct msghdr msg = {0};
    struct cmsghdr *cmsg;
    struct iovec iov;
    int fd = -1;
    ssize_t n;

    iov.iov_base = desc;
    iov.iov_len = sizeof(*desc);
    msg.msg_iov = &iov;
    msg.msg_iovlen = 1;
    msg.msg_control = control;
    msg.msg_controllen = sizeof(control);

    n = recvmsg(channel_fd, &msg, MSG_DONTWAIT | MSG_CMSG_CLOEXEC);
    if (n == 0) return -1;                       /* peer closed the channel */
    if (n != (ssize_t)sizeof(*desc)) return 0;   /* EAGAIN or short read: nothing usable now */

    for (cmsg = CMSG_FIRSTHDR(&msg); cmsg; cmsg = CMSG_NXTHDR(&msg, cmsg))
        if (cmsg->cmsg_level == SOL_SOCKET && cmsg->cmsg_type == SCM_RIGHTS)
            memcpy(&fd, CMSG_DATA(cmsg), sizeof(fd));
    *out_fd = fd;
    return 1;
}

/* Build a wl_buffer wrapping the dmabuf fd plus its tracking buffer, cached on the
 * surface. Does not close fd (caller owns it). Returns NULL on failure. */
static BOOL wayland_hwnd_shm_format_from_fourcc(unsigned int fourcc, uint32_t *format)
{
    switch (fourcc)
    {
    case HWND_DMABUF_SHM_FORMAT_XRGB8888:
        if (format) *format = WL_SHM_FORMAT_XRGB8888;
        return TRUE;
    case HWND_DMABUF_SHM_FORMAT_ARGB8888:
        if (format) *format = WL_SHM_FORMAT_ARGB8888;
        return TRUE;
    default:
        return FALSE;
    }
}

static struct wl_buffer *wayland_hwnd_dmabuf_create_shm_wl_buffer(
        struct wayland_hwnd_dmabuf_surface *surface, const hwnd_dmabuf_frame_desc_t *desc, int fd)
{
    struct wl_shm_pool *pool;
    struct wl_buffer *wl_buffer;
    uint32_t format;
    UINT64 size;

    if (!process_wayland.wl_shm || fd < 0) return NULL;
    if (!wayland_hwnd_shm_format_from_fourcc(desc->fourcc, &format)) return NULL;
    if (desc->width > INT_MAX / 4 || desc->height > INT_MAX || desc->stride < desc->width * 4) return NULL;
    if (!desc->stride || desc->height > (UINT_MAX - desc->offset) / desc->stride) return NULL;
    size = desc->offset + (UINT64)desc->stride * desc->height;
    if (!size || size > INT_MAX) return NULL;

    if (!(pool = wl_shm_create_pool(process_wayland.wl_shm, fd, (int)size)))
        return NULL;
    wl_buffer = wl_shm_pool_create_buffer(pool, desc->offset, desc->width, desc->height,
                                          desc->stride, format);
    wl_shm_pool_destroy(pool);
    if (!wl_buffer)
        WARN("hwnd=%p failed to create shm buffer size=%ux%u stride=%u fourcc=%#x format=%#x\n",
             surface->hwnd, desc->width, desc->height, desc->stride, desc->fourcc, format);
    return wl_buffer;
}

static struct wayland_hwnd_dmabuf_buffer *wayland_hwnd_dmabuf_create_buffer(
        struct wayland_hwnd_dmabuf_surface *surface, const hwnd_dmabuf_frame_desc_t *desc,
        int fd, BOOL stable_slot)
{
    struct wayland_hwnd_dmabuf_buffer *buffer;
    if (!(buffer = calloc(1, sizeof(*buffer)))) return NULL;
    buffer->channel_fd = -1;
    buffer->wl_buffer = wayland_hwnd_dmabuf_create_shm_wl_buffer(surface, desc, fd);
    if (!buffer->wl_buffer)
    {
        free(buffer);
        return NULL;
    }
    buffer->ref = 1;  /* owner ref. A compositor ref is added per attach in the update loop */
    buffer->surface = surface;
    buffer->producer_unique_id = desc->producer_unique_id;
    buffer->image_id = desc->image_id;
    buffer->ring_generation = desc->ring_generation;
    buffer->fourcc = desc->fourcc;
    buffer->stride = desc->stride;
    buffer->offset = desc->offset;
    buffer->modifier = desc->modifier;
    buffer->width = desc->width;
    buffer->height = desc->height;
    buffer->stable_slot = stable_slot;
    buffer->cache_valid = stable_slot ? TRUE : FALSE;
    buffer->release_flags = HWND_DMABUF_RELEASE_ORPHANED;
    buffer->channel_fd = surface->channel_fd >= 0 ? dup(surface->channel_fd) : -1;
    wl_list_insert(surface->buffers.prev, &buffer->link);
    wl_buffer_add_listener(buffer->wl_buffer, &wayland_hwnd_dmabuf_buffer_listener, buffer);
    return buffer;
}

/* Make buffer the surface's current frame and publish its (new) contents. */
static void wayland_hwnd_dmabuf_attach_frame(struct wayland_hwnd_dmabuf_surface *surface,
        struct wayland_hwnd_dmabuf_buffer *buffer, const hwnd_dmabuf_frame_desc_t *desc)
{
    if (wayland_hwnd_dmabuf_buffer_exchange_release_token(buffer, desc->release_token))
        WARN("hwnd=%p slot=%u overwrote an unreleased token\n", surface->hwnd, desc->image_id);
    buffer->alpha_mode = desc->alpha_mode;
    buffer->release_flags = HWND_DMABUF_RELEASE_PRESENTED;
    surface->current = buffer;
    surface->frame_seq = desc->frame_seq;
    wl_surface_attach(surface->wl_surface, buffer->wl_buffer, 0, 0);
    wl_surface_damage_buffer(surface->wl_surface, 0, 0, desc->width, desc->height);
    wp_viewport_set_source(surface->wp_viewport, 0, 0,
                           wl_fixed_from_int(desc->width), wl_fixed_from_int(desc->height));
}

static void wayland_hwnd_dmabuf_drop_current_frame(struct wayland_hwnd_dmabuf_surface *surface,
                                                   unsigned int flags)
{
    struct wayland_hwnd_dmabuf_buffer *buffer = surface->current;

    if (!buffer) return;
    /* An attached buffer remains immutable until wl_buffer.release, even when
     * its window becomes hidden or clipped. Only an uncommitted frame can be
     * returned to the producer immediately. */
    if (InterlockedCompareExchange(&buffer->ref, 0, 0) == 1)
        wayland_hwnd_dmabuf_buffer_send_release(buffer, flags, buffer->stable_slot);
    surface->current = NULL;
    wl_surface_attach(surface->wl_surface, NULL, 0, 0);
    wl_surface_commit(surface->wl_surface);
    if (!buffer->stable_slot) wayland_hwnd_dmabuf_buffer_reap(buffer);
}

/* Stable-slot path (HWND_DMABUF_FLAG_STABLE_SLOT): ensure this slot is imported and cached
 * (no attach). The producer sends a slot's dmabuf fd only once, then fd-less references:
 *  - cache hit (exact layout match): reuse the cached wl_buffer.
 *  - fd-bearing miss: import and cache it.
 *  - fd-less miss: the slot is not cached -> return NULL. The failed release clears the
 *    producer's cache state so it resends the fd.
 * Stale slots (new producer/ring generation or a changed layout) are reaped first. */
static struct wayland_hwnd_dmabuf_buffer *wayland_hwnd_dmabuf_cache_slot(
        struct wayland_hwnd_dmabuf_surface *surface, const hwnd_dmabuf_frame_desc_t *desc,
        int fd, BOOL *created_buffer)
{
    struct wayland_hwnd_dmabuf_buffer *buffer = NULL, *it, *it_next;

    wl_list_for_each_safe(it, it_next, &surface->buffers, link)
        if (it->producer_unique_id != desc->producer_unique_id ||
            it->ring_generation != desc->ring_generation)
        {
            TRACE("hwnd=%p cache-drop stale producer/gen slot=%u gen %u->%u\n",
                  surface->hwnd, it->image_id, it->ring_generation, desc->ring_generation);
            wayland_hwnd_dmabuf_buffer_reap(it);
        }

    wl_list_for_each_safe(it, it_next, &surface->buffers, link)
    {
        if (it->image_id != desc->image_id) continue;
        if (it->width == (int)desc->width && it->height == (int)desc->height &&
            it->fourcc == desc->fourcc && it->modifier == desc->modifier &&
            it->stride == desc->stride && it->offset == desc->offset)
            buffer = it;
        else
        {
            TRACE("hwnd=%p cache-drop layout-mismatch slot=%u\n", surface->hwnd, it->image_id);
            wayland_hwnd_dmabuf_buffer_reap(it);
        }
        break;
    }

    if (buffer)
    {
        TRACE("hwnd=%p cache-hit slot=%u gen=%u frame_seq=%u\n",
              surface->hwnd, desc->image_id, desc->ring_generation, desc->frame_seq);
        return buffer;
    }
    if (fd < 0)
    {
        TRACE("hwnd=%p cache-miss-no-fd slot=%u (awaiting fd resend)\n",
              surface->hwnd, desc->image_id);
        return NULL;
    }
    if (!(buffer = wayland_hwnd_dmabuf_create_buffer(surface, desc, fd, TRUE))) return NULL;
    TRACE("hwnd=%p cache-miss import slot=%u gen=%u frame_seq=%u\n",
          surface->hwnd, desc->image_id, desc->ring_generation, desc->frame_seq);
    *created_buffer = TRUE;
    return buffer;
}

/* A queued frame superseded by a newer one. For a stable slot, still import/cache it so a
 * later reference resolves. Then release the present so the producer can recycle the slot.
 * Uncached frames are simply dropped (each is a fresh dmabuf, nothing to keep). */
static void wayland_hwnd_dmabuf_retire_frame(struct wayland_hwnd_dmabuf_surface *surface,
        const hwnd_dmabuf_frame_desc_t *desc, int fd, BOOL *created_buffer)
{
    struct wayland_hwnd_dmabuf_buffer *buffer = NULL;
    unsigned int flags = HWND_DMABUF_RELEASE_DROPPED;

    if (desc->flags & HWND_DMABUF_FLAG_STABLE_SLOT)
    {
        buffer = wayland_hwnd_dmabuf_cache_slot(surface, desc, fd, created_buffer);
        if (buffer) flags |= HWND_DMABUF_RELEASE_CACHED;
        else flags = HWND_DMABUF_RELEASE_FAILED;
    }
    if (fd >= 0) close(fd);
    wayland_hwnd_dmabuf_send_release(surface, desc->producer_unique_id, desc->release_token,
                                     flags, desc->image_id, desc->ring_generation);
}

/* Drain the channel, importing every fd-bearing slot so none is lost, then show the newest
 * frame. Stable-slot producers send a slot's fd once and fd-less references after. The fd
 * is dup'd + passed via SCM_RIGHTS once per slot, not once per frame. created_buffer reports
 * a freshly imported wl_buffer. attached_frame reports a frame was published. */
static struct wayland_hwnd_dmabuf_buffer *wayland_hwnd_dmabuf_surface_import_buffer(
        struct wayland_hwnd_dmabuf_surface *surface, const hwnd_dmabuf_frame_info_t *info,
        BOOL *created_buffer, BOOL *attached_frame)
{
    struct wayland_hwnd_dmabuf_buffer *buffer = NULL;
    hwnd_dmabuf_frame_desc_t pdesc, desc;
    BOOL have_pending = FALSE, reclaimed_channel = FALSE;
    int pfd = -1, fd, r;

    *created_buffer = FALSE;
    *attached_frame = FALSE;
    memset(&pdesc, 0, sizeof(pdesc));

    wayland_hwnd_dmabuf_surface_claim_channel(surface);
    if (surface->channel_fd < 0) return surface->current;

retry_channel:
    while ((r = wayland_hwnd_dmabuf_channel_recv_one(surface->channel_fd, &desc, &fd)) > 0)
    {
        if (desc.version != HWND_DMABUF_DESC_VERSION_V1 || !desc.width || !desc.height ||
            !desc.stride || !desc.fourcc || !desc.release_token ||
            (!(desc.flags & HWND_DMABUF_FLAG_SHM) || !wayland_hwnd_shm_format_from_fourcc(desc.fourcc, NULL)))
        {
            WARN("hwnd=%p import rejected info_seq=%u desc_seq=%u fourcc=%#x\n",
                 surface->hwnd, info->frame_seq, desc.frame_seq, desc.fourcc);
            if (fd >= 0) close(fd);
            if (desc.release_token)
                wayland_hwnd_dmabuf_send_release(surface, desc.producer_unique_id, desc.release_token,
                                                 HWND_DMABUF_RELEASE_FAILED,
                                                 desc.image_id, desc.ring_generation);
            continue;
        }
        if (have_pending)
            wayland_hwnd_dmabuf_retire_frame(surface, &pdesc, pfd, created_buffer);
        pdesc = desc;
        pfd = fd;
        have_pending = TRUE;
    }

    if (r < 0)
    {
        close(surface->channel_fd);
        surface->channel_fd = -1;
        /* Resize can replace the server channel before this wake is handled.
         * Read the replacement now: a static window may publish just one frame. */
        if (!reclaimed_channel)
        {
            reclaimed_channel = TRUE;
            wayland_hwnd_dmabuf_surface_claim_channel(surface);
            if (surface->channel_fd >= 0) goto retry_channel;
        }
        if (!have_pending)
            wayland_hwnd_dmabuf_drop_current_frame(surface, HWND_DMABUF_RELEASE_ORPHANED);
    }
    if (!have_pending) return surface->current;

    /* Show the newest drained frame. */
    if (pdesc.flags & HWND_DMABUF_FLAG_STABLE_SLOT)
        buffer = wayland_hwnd_dmabuf_cache_slot(surface, &pdesc, pfd, created_buffer);
    else if ((buffer = wayland_hwnd_dmabuf_create_buffer(surface, &pdesc, pfd, FALSE)))
        *created_buffer = TRUE;

    if (pfd >= 0) close(pfd);

    if (!buffer)
    {
        wayland_hwnd_dmabuf_send_release(surface, pdesc.producer_unique_id, pdesc.release_token,
                                         HWND_DMABUF_RELEASE_FAILED,
                                         pdesc.image_id, pdesc.ring_generation);
        return surface->current;
    }
    wayland_hwnd_dmabuf_attach_frame(surface, buffer, &pdesc);
    *attached_frame = TRUE;
    return buffer;
}

void wayland_hwnd_gdi_update(struct wayland_surface *surface)
{
    hwnd_dmabuf_frame_info_t stack_frames[16], *frames = stack_frames;
    unsigned int total = 0, count = 0, i;
    struct wayland_hwnd_dmabuf_buffer *buffer, *buffer_next;
    struct wayland_hwnd_dmabuf_surface *dmabuf_surface, *next;
    enum hwnd_dmabuf_status status;
    struct wayland_win_data *data;
    struct wl_surface *above;
    unsigned long long now = wayland_time_ms();
    BOOL any_new = FALSE;

    if (!process_wayland.wl_shm) return;
    /* dmabuf children are composited for primary surfaces. */
    if (!wayland_surface_is_toplevel(surface))
        return;

    /* Import is driven by producer wakes (WM_WAYLAND_DMABUF_FRAME) at the producer's
     * self-paced rate, regardless of whether our toplevel is presented. We do not pace
     * to a frame callback on our surface: the compositor stops firing it when the surface
     * is not in front, stalling delivery while another window (e.g. a game) is active. */

    wl_list_for_each(dmabuf_surface, &surface->hwnd_dmabuf_surfaces, link)
        dmabuf_surface->seen = FALSE;

    status = wine_hwnd_dmabuf_list(surface->hwnd, frames, ARRAY_SIZE(stack_frames), &total, &count);
    if (status != HWND_DMABUF_OK)
    {
        TRACE("hwnd=%p wine_hwnd_dmabuf_list status=%u\n", surface->hwnd, status);
        return;
    }
    if (total > count)
    {
        if (!(frames = calloc(total, sizeof(*frames)))) return;
        status = wine_hwnd_dmabuf_list(surface->hwnd, frames, total, &total, &count);
        if (status != HWND_DMABUF_OK) count = 0;
    }

    TRACE("hwnd=%p listed total=%u count=%u\n", surface->hwnd, total, count);

    /* Anchor the dmabuf chain above the client subsurface, not the main surface.
     * Both used to place_above() main. Whichever committed last won the top,
     * a per-frame z-order race (the flicker). A fixed sibling anchor makes
     * [main, client, dmabuf...] resolve the same regardless. (win_data_mutex held.) */
    above = surface->wl_surface;
    if ((data = wayland_win_data_get_nolock(surface->hwnd)) &&
        data->client_surface && data->client_surface->wl_subsurface &&
        data->client_surface->toplevel == surface->hwnd)
        above = data->client_surface->wl_surface;
    /* Server child lists are returned in z-order from top to bottom, while
     * Wayland subsurface stacking needs to be applied from bottom to top when
     * chaining place_above() calls. */
    for (i = count; i-- > 0; )
    {
        HWND hwnd = (HWND)(UINT_PTR)frames[i].hwnd;
        BOOL created_buffer, attached_frame;

        TRACE("hwnd=%p child=%p frame_seq=%u opened=%u\n",
              surface->hwnd, hwnd, frames[i].frame_seq, frames[i].opened);

        if (!(dmabuf_surface = wayland_hwnd_dmabuf_surface_get(surface, hwnd)) &&
            !(dmabuf_surface = wayland_hwnd_dmabuf_surface_create(surface, hwnd)))
            continue;

        dmabuf_surface->seen = TRUE;
        dmabuf_surface->last_seen_ms = now;
        /* Reap uncached buffers the compositor has released. Cached stable-slot
         * buffers set no released flag and stay cached for reuse. */
        wl_list_for_each_safe(buffer, buffer_next, &dmabuf_surface->buffers, link)
            if (buffer->released)
                wayland_hwnd_dmabuf_buffer_reap(buffer);

        wayland_hwnd_dmabuf_surface_import_buffer(dmabuf_surface, &frames[i],
                                                  &created_buffer, &attached_frame);
        if (attached_frame) any_new = TRUE;
        if (!dmabuf_surface->current) continue;
        if (!wayland_hwnd_dmabuf_surface_configure(surface, dmabuf_surface, &frames[i], above))
        {
            TRACE("hwnd=%p child=%p configure failed frame_seq=%u\n",
                  surface->hwnd, hwnd, frames[i].frame_seq);
            wayland_hwnd_dmabuf_drop_current_frame(dmabuf_surface, HWND_DMABUF_RELEASE_DROPPED);
            continue;
        }

        /* One compositor ref per attached frame. The wl_buffer.release handler drops
         * it. Cached slots re-attach the same wl_buffer each new frame, hence the ref
         * is per-attach. The busy gate keeps at most one attach of a slot outstanding. */
        if (attached_frame)
            InterlockedIncrement(&dmabuf_surface->current->ref);
        wl_surface_commit(dmabuf_surface->wl_surface);
        /* Geometry wakes also need a parent commit to apply subsurface moves. */
        any_new = TRUE;
        above = dmabuf_surface->wl_surface;
    }

    /* record the chain top for the GDI overlay anchor, see wayland_surface_overlay_anchor */


    /* A child absent from the descendant list: hide its stale overlay at once so the
     * content behind it (e.g. the client surface) is not occluded. Keep the dmabuf
     * cache for a grace window so a brief gap does not force a re-import. Reap the
     * cache only once the grace expires. */
    wl_list_for_each_safe(dmabuf_surface, next, &surface->hwnd_dmabuf_surfaces, link)
    {
        if (dmabuf_surface->seen) continue;
        if (now - dmabuf_surface->last_seen_ms > WAYLAND_DMABUF_SURFACE_GRACE_MS)
        {
            wayland_hwnd_dmabuf_surface_destroy(dmabuf_surface);
            any_new = TRUE;
        }
        else if (dmabuf_surface->current)
        {
            wl_surface_attach(dmabuf_surface->wl_surface, NULL, 0, 0);
            wl_surface_commit(dmabuf_surface->wl_surface);
            dmabuf_surface->current = NULL;
            any_new = TRUE;
        }
    }

    /* Commit and arm the next vsync only when something changed. An idle window costs nothing. */
    /* Commit the parent to apply subsurface stacking. No frame callback: import is
     * paced by the producer, not by our toplevel being presented. */
    if (any_new)
        wl_surface_commit(surface->wl_surface);
    if (frames != stack_frames) free(frames);
}


void wayland_hwnd_gdi_clear(struct wayland_surface *surface)
{
    struct wayland_hwnd_dmabuf_surface *child, *next;
    wl_list_for_each_safe(child, next, &surface->hwnd_dmabuf_surfaces, link)
        wayland_hwnd_dmabuf_surface_destroy(child);
}
