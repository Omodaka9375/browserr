//! D3D12 shared-frame producer: renders each frame into a shared BGRA8
//! texture (named, per size) plus a shared fence. The chrome imports them via
//! `NativeSurface::push_shared`, so no pixel bytes cross the socket.
//!
//! Contract (rune native_surface::windows): BGRA8, 1 mip, 1 sample, one array
//! slice; texture and fence shared by name; the importer signals the fence to
//! the pushed value once it has copied the frame.
//!
//! Upload path: CPU -> persistent-mapped upload buffer -> CopyBufferToTexture
//! on our own queue (a CPU-write texture cannot be shared on discrete GPUs).

use windows::Win32::Foundation::{CloseHandle, GENERIC_ALL, HANDLE};
use windows::Win32::Graphics::Direct3D::D3D_FEATURE_LEVEL_11_0;
use windows::Win32::Graphics::Direct3D12::{
    D3D12_BOX, D3D12_COMMAND_LIST_TYPE_DIRECT, D3D12_COMMAND_QUEUE_DESC,
    D3D12_CPU_PAGE_PROPERTY_UNKNOWN, D3D12_FENCE_FLAG_NONE, D3D12_FENCE_FLAG_SHARED,
    D3D12_HEAP_FLAG_NONE, D3D12_HEAP_FLAG_SHARED, D3D12_HEAP_FLAGS, D3D12_HEAP_PROPERTIES,
    D3D12_HEAP_TYPE_DEFAULT, D3D12_HEAP_TYPE_UPLOAD, D3D12_MEMORY_POOL_UNKNOWN,
    D3D12_PLACED_SUBRESOURCE_FOOTPRINT, D3D12_RESOURCE_BARRIER, D3D12_RESOURCE_BARRIER_0,
    D3D12_RESOURCE_BARRIER_FLAG_NONE, D3D12_RESOURCE_BARRIER_TYPE_TRANSITION, D3D12_RESOURCE_DESC,
    D3D12_RESOURCE_DIMENSION_BUFFER, D3D12_RESOURCE_DIMENSION_TEXTURE2D,
    D3D12_RESOURCE_FLAG_ALLOW_SIMULTANEOUS_ACCESS, D3D12_RESOURCE_FLAG_NONE,
    D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATES,
    D3D12_RESOURCE_TRANSITION_BARRIER, D3D12_SUBRESOURCE_FOOTPRINT, D3D12_TEXTURE_COPY_LOCATION,
    D3D12_TEXTURE_COPY_LOCATION_0, D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT,
    D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX, D3D12_TEXTURE_LAYOUT_ROW_MAJOR,
    D3D12_TEXTURE_LAYOUT_UNKNOWN, ID3D12PipelineState,
};
use windows::Win32::Graphics::Direct3D12::{
    D3D12_RESOURCE_STATE_GENERIC_READ, D3D12CreateDevice, ID3D12CommandAllocator,
    ID3D12CommandList, ID3D12CommandQueue, ID3D12Device, ID3D12DeviceChild, ID3D12Fence,
    ID3D12GraphicsCommandList, ID3D12Resource,
};
use windows::Win32::Graphics::Dxgi::Common::{DXGI_FORMAT_B8G8R8A8_UNORM, DXGI_SAMPLE_DESC};
use windows::Win32::System::Threading::{CreateEventW, WaitForSingleObject};
use windows::core::Interface;

/// Instance counter for pool names: resizing back to a previous size must not
/// reuse a name whose kernel object is still held (e.g. by the chrome's
/// importer cache) - that fails CreateSharedHandle with NAME_ALREADY_EXISTS.
static POOL_SEQ: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);

pub struct Pool {
    device: ID3D12Device,
    queue: ID3D12CommandQueue,
    allocator: ID3D12CommandAllocator,
    list: ID3D12GraphicsCommandList,
    resource: ID3D12Resource,
    upload: ID3D12Resource,
    upload_ptr: *mut u8,
    our_fence: ID3D12Fence,
    rune_fence: ID3D12Fence,
    fence_event: HANDLE,
    frame_name: String,
    fence_name: String,
    size: (u32, u32),
    /// Rune-fence value of the last pushed frame (0 = nothing pushed yet).
    pushed: u64,
    /// Our-queue fence value of the last upload.
    submitted: u64,
}

fn share_by_name(device: &ID3D12Device, obj: &ID3D12DeviceChild, name: &str) -> Result<(), String> {
    let name_wide: Vec<u16> = name.encode_utf16().chain(std::iter::once(0)).collect();
    unsafe {
        device
            .CreateSharedHandle(
                obj,
                None,
                GENERIC_ALL.0,
                windows::core::PCWSTR(name_wide.as_ptr()),
            )
            .map_err(|e| format!("CreateSharedHandle({name}): {e}"))?;
    }
    Ok(())
}

/// Bounded CPU wait until `fence` reaches `value`.
fn wait_fence(fence: &ID3D12Fence, value: u64, event: HANDLE, timeout_ms: u32) {
    unsafe {
        if fence.GetCompletedValue() < value && fence.SetEventOnCompletion(value, event).is_ok() {
            WaitForSingleObject(event, timeout_ms);
        }
    }
}

impl Pool {
    /// Create a D3D12 device (default adapter), a shared texture + rune fence,
    /// and the upload pipeline, sized `w x h`.
    pub fn new(w: u32, h: u32) -> Result<Pool, String> {
        unsafe {
            let mut device: Option<ID3D12Device> = None;
            D3D12CreateDevice(
                None::<&windows::core::IUnknown>,
                D3D_FEATURE_LEVEL_11_0,
                &mut device,
            )
            .map_err(|e| format!("D3D12CreateDevice: {e}"))?;
            let device = device.ok_or("no D3D12 device")?;

            // The shared texture: DEFAULT heap + SHARED + SIMULTANEOUS_ACCESS.
            let desc = D3D12_RESOURCE_DESC {
                Dimension: D3D12_RESOURCE_DIMENSION_TEXTURE2D,
                Alignment: 0,
                Width: w as u64,
                Height: h,
                DepthOrArraySize: 1,
                MipLevels: 1,
                Format: DXGI_FORMAT_B8G8R8A8_UNORM,
                SampleDesc: DXGI_SAMPLE_DESC {
                    Count: 1,
                    Quality: 0,
                },
                Layout: D3D12_TEXTURE_LAYOUT_UNKNOWN,
                Flags: D3D12_RESOURCE_FLAG_ALLOW_SIMULTANEOUS_ACCESS,
            };
            let heap = D3D12_HEAP_PROPERTIES {
                Type: D3D12_HEAP_TYPE_DEFAULT,
                CPUPageProperty: D3D12_CPU_PAGE_PROPERTY_UNKNOWN,
                MemoryPoolPreference: D3D12_MEMORY_POOL_UNKNOWN,
                CreationNodeMask: 0,
                VisibleNodeMask: 0,
            };
            let resource = create_committed::<ID3D12Resource>(
                &device,
                &heap,
                &desc,
                D3D12_HEAP_FLAG_SHARED,
                D3D12_RESOURCE_STATE_COMMON,
            )
            .map_err(|e| format!("shared texture: {e}"))?;

            let queue_desc = D3D12_COMMAND_QUEUE_DESC {
                Type: D3D12_COMMAND_LIST_TYPE_DIRECT,
                ..Default::default()
            };
            let queue = device
                .CreateCommandQueue::<ID3D12CommandQueue>(&queue_desc)
                .map_err(|e| format!("CreateCommandQueue: {e}"))?;
            let allocator = device
                .CreateCommandAllocator::<ID3D12CommandAllocator>(D3D12_COMMAND_LIST_TYPE_DIRECT)
                .map_err(|e| format!("CreateCommandAllocator: {e}"))?;
            let list: ID3D12GraphicsCommandList = device
                .CreateCommandList(
                    0,
                    D3D12_COMMAND_LIST_TYPE_DIRECT,
                    &allocator,
                    None::<&ID3D12PipelineState>,
                )
                .map_err(|e| format!("CreateCommandList: {e}"))?;
            list.Close().map_err(|e| format!("Close list: {e}"))?;

            // Upload buffer: a plain buffer in an upload heap, persistently mapped.
            let upload_desc = D3D12_RESOURCE_DESC {
                Dimension: D3D12_RESOURCE_DIMENSION_BUFFER,
                Alignment: 0,
                Width: (w as usize * h as usize * 4) as u64,
                Height: 1,
                DepthOrArraySize: 1,
                MipLevels: 1,
                Format: Default::default(),
                SampleDesc: DXGI_SAMPLE_DESC {
                    Count: 1,
                    Quality: 0,
                },
                Layout: D3D12_TEXTURE_LAYOUT_ROW_MAJOR, // buffers require ROW_MAJOR
                Flags: D3D12_RESOURCE_FLAG_NONE,
            };
            let upload_heap = D3D12_HEAP_PROPERTIES {
                Type: D3D12_HEAP_TYPE_UPLOAD,
                CPUPageProperty: D3D12_CPU_PAGE_PROPERTY_UNKNOWN,
                MemoryPoolPreference: D3D12_MEMORY_POOL_UNKNOWN,
                CreationNodeMask: 0,
                VisibleNodeMask: 0,
            };
            let upload = create_committed::<ID3D12Resource>(
                &device,
                &upload_heap,
                &upload_desc,
                D3D12_HEAP_FLAG_NONE,
                D3D12_RESOURCE_STATE_GENERIC_READ,
            )
            .map_err(|e| format!("upload buffer: {e}"))?;
            let mut data_ptr: *mut core::ffi::c_void = std::ptr::null_mut();
            upload
                .Map(0, None, Some(&mut data_ptr))
                .map_err(|e| format!("Map upload buffer: {e}"))?;
            let upload_ptr = data_ptr as *mut u8;

            // Rune's fence: shared + named, signaled by the chrome's importer.
            let rune_fence = device
                .CreateFence::<ID3D12Fence>(0, D3D12_FENCE_FLAG_SHARED)
                .map_err(|e| format!("create rune fence: {e}"))?;
            // Ours: queue-completion tracking, no sharing needed.
            let our_fence = device
                .CreateFence::<ID3D12Fence>(0, D3D12_FENCE_FLAG_NONE)
                .map_err(|e| format!("create our fence: {e}"))?;

            let instance = POOL_SEQ.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
            let frame_name = format!("browserr-frame-{w}x{h}-{instance}");
            let fence_name = format!("browserr-fence-{w}x{h}-{instance}");
            let texture_child: ID3D12DeviceChild =
                resource.cast().map_err(|e| format!("cast resource: {e}"))?;
            share_by_name(&device, &texture_child, &frame_name)?;
            let fence_child: ID3D12DeviceChild =
                rune_fence.cast().map_err(|e| format!("cast fence: {e}"))?;
            share_by_name(&device, &fence_child, &fence_name)?;

            let fence_event =
                CreateEventW(None, false, false, None).map_err(|e| format!("CreateEventW: {e}"))?;

            Ok(Pool {
                device,
                queue,
                allocator,
                list,
                resource,
                upload,
                upload_ptr,
                our_fence,
                rune_fence,
                fence_event,
                frame_name,
                fence_name,
                size: (w, h),
                pushed: 0,
                submitted: 0,
            })
        }
    }

    pub fn size(&self) -> (u32, u32) {
        self.size
    }

    /// Adapter LUID of the underlying D3D12 device (debug aid).
    pub fn adapter_luid(&self) -> String {
        let luid = unsafe { self.device.GetAdapterLuid() };
        format!("{:08x}-{:08x}", luid.HighPart, luid.LowPart)
    }

    /// (frame texture name, fence name, fence value to push with this frame).
    /// Call after a successful `write`.
    pub fn info(&self) -> (String, String, u64) {
        (
            self.frame_name.clone(),
            self.fence_name.clone(),
            self.pushed,
        )
    }

    /// Upload `bgra` into the shared texture. Waits (bounded) until the chrome
    /// has consumed the previous frame and our previous copy has completed.
    pub fn write(&mut self, bgra: &[u8]) -> Result<(), String> {
        let (w, h) = self.size;
        let size = (w as usize * h as usize * 4) as u64;
        if bgra.len() as u64 != size {
            return Err(format!("frame size {} != expected {size}", bgra.len()));
        }
        unsafe {
            // Chrome signals the rune fence once it has copied; our fence
            // covers the previous GPU upload.
            if self.pushed > 0 {
                wait_fence(&self.rune_fence, self.pushed, self.fence_event, 100);
            }
            if self.submitted > 0 {
                wait_fence(&self.our_fence, self.submitted, self.fence_event, 100);
            }
            std::ptr::copy_nonoverlapping(bgra.as_ptr(), self.upload_ptr, bgra.len());

            self.list
                .Reset(&self.allocator, None)
                .map_err(|e| format!("list reset: {e}"))?;
            let barrier = |before: D3D12_RESOURCE_STATES, after: D3D12_RESOURCE_STATES| {
                D3D12_RESOURCE_BARRIER {
                    Type: D3D12_RESOURCE_BARRIER_TYPE_TRANSITION,
                    Flags: D3D12_RESOURCE_BARRIER_FLAG_NONE,
                    Anonymous: D3D12_RESOURCE_BARRIER_0 {
                        Transition: std::mem::ManuallyDrop::new(
                            D3D12_RESOURCE_TRANSITION_BARRIER {
                                pResource: std::mem::ManuallyDrop::new(Some(self.resource.clone())),
                                Subresource: 0,
                                StateBefore: before,
                                StateAfter: after,
                            },
                        ),
                    },
                }
            };
            self.list.ResourceBarrier(&[barrier(
                D3D12_RESOURCE_STATE_COMMON,
                D3D12_RESOURCE_STATE_COPY_DEST,
            )]);
            let dst = D3D12_TEXTURE_COPY_LOCATION {
                pResource: std::mem::ManuallyDrop::new(Some(self.resource.clone())),
                Type: D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX,
                Anonymous: D3D12_TEXTURE_COPY_LOCATION_0 {
                    SubresourceIndex: 0,
                },
            };
            let src = D3D12_TEXTURE_COPY_LOCATION {
                pResource: std::mem::ManuallyDrop::new(Some(self.upload.clone())),
                Type: D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT,
                Anonymous: D3D12_TEXTURE_COPY_LOCATION_0 {
                    PlacedFootprint: D3D12_PLACED_SUBRESOURCE_FOOTPRINT {
                        Offset: 0,
                        Footprint: D3D12_SUBRESOURCE_FOOTPRINT {
                            Format: DXGI_FORMAT_B8G8R8A8_UNORM,
                            Width: w,
                            Height: h,
                            Depth: 1,
                            RowPitch: w * 4,
                        },
                    },
                },
            };
            self.list
                .CopyTextureRegion(&dst, 0, 0, 0, &src, None::<*const D3D12_BOX>);
            self.list.ResourceBarrier(&[barrier(
                D3D12_RESOURCE_STATE_COPY_DEST,
                D3D12_RESOURCE_STATE_COMMON,
            )]);
            self.list.Close().map_err(|e| format!("list close: {e}"))?;

            let command_list: ID3D12CommandList =
                self.list.cast().map_err(|e| format!("cast list: {e}"))?;
            self.queue.ExecuteCommandLists(&[Some(command_list)]);
            self.submitted += 1;
            self.queue
                .Signal(&self.our_fence, self.submitted)
                .map_err(|e| format!("queue signal: {e}"))?;
            self.pushed += 1;
        }
        Ok(())
    }
}

fn create_committed<T: Interface>(
    device: &ID3D12Device,
    heap: &D3D12_HEAP_PROPERTIES,
    desc: &D3D12_RESOURCE_DESC,
    heap_flags: D3D12_HEAP_FLAGS,
    initial_state: D3D12_RESOURCE_STATES,
) -> Result<T, String> {
    unsafe {
        let mut out: Option<T> = None;
        device
            .CreateCommittedResource(heap, heap_flags, desc, initial_state, None, &mut out)
            .map_err(|e| format!("CreateCommittedResource: {e}"))?;
        out.ok_or_else(|| "CreateCommittedResource returned nothing".to_string())
    }
}

impl Drop for Pool {
    fn drop(&mut self) {
        unsafe {
            let _ = CloseHandle(self.fence_event);
        }
    }
}
