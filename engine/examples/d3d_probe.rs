//! Probe: which D3D12 heap/flag combos produce a shared, CPU-writable BGRA
//! texture? Tries all plausible combos for a 64x64 texture and reports
//! create + WriteToSubresource results, plus adapter info.

use windows::Win32::Foundation::{CloseHandle, GENERIC_ALL, HANDLE};
use windows::Win32::Graphics::Direct3D::D3D_FEATURE_LEVEL_11_0;
use windows::Win32::Graphics::Direct3D12::{
    D3D12_BOX, D3D12_COMMAND_LIST_TYPE_DIRECT, D3D12_CPU_PAGE_PROPERTY_UNKNOWN,
    D3D12_CPU_PAGE_PROPERTY_WRITE_COMBINE, D3D12_FENCE_FLAG_SHARED, D3D12_HEAP_FLAG_SHARED,
    D3D12_HEAP_PROPERTIES, D3D12_HEAP_TYPE_CUSTOM, D3D12_HEAP_TYPE_DEFAULT, D3D12_HEAP_TYPE_UPLOAD,
    D3D12_MEMORY_POOL_L0, D3D12_MEMORY_POOL_UNKNOWN, D3D12_RESOURCE_DESC,
    D3D12_RESOURCE_DIMENSION_BUFFER, D3D12_RESOURCE_DIMENSION_TEXTURE2D,
    D3D12_RESOURCE_FLAG_ALLOW_SIMULTANEOUS_ACCESS, D3D12_RESOURCE_FLAG_DENY_SHADER_RESOURCE,
    D3D12_RESOURCE_FLAGS, D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATES,
    D3D12_TEXTURE_LAYOUT_ROW_MAJOR, D3D12_TEXTURE_LAYOUT_UNKNOWN,
};
use windows::Win32::Graphics::Direct3D12::{
    D3D12CreateDevice, ID3D12Device, ID3D12DeviceChild, ID3D12Fence, ID3D12Resource,
};
use windows::Win32::Graphics::Dxgi::Common::{DXGI_FORMAT_B8G8R8A8_UNORM, DXGI_SAMPLE_DESC};
use windows::Win32::System::Threading::{CreateEventW, WaitForSingleObject};
use windows::core::Interface;

struct Combo {
    label: &'static str,
    heap_type: windows::Win32::Graphics::Direct3D12::D3D12_HEAP_TYPE,
    cpu_page: windows::Win32::Graphics::Direct3D12::D3D12_CPU_PAGE_PROPERTY,
    pool: windows::Win32::Graphics::Direct3D12::D3D12_MEMORY_POOL,
    flags: windows::Win32::Graphics::Direct3D12::D3D12_RESOURCE_FLAGS,
    shared: bool,
}

fn main() {
    unsafe {
        // Debug layer: validation messages land in the info queue below.
        let enable_debug = std::env::var("PROBE_DEBUG").is_ok();
        let mut debug: Option<windows::Win32::Graphics::Direct3D12::ID3D12Debug> = None;
        if enable_debug
            && windows::Win32::Graphics::Direct3D12::D3D12GetDebugInterface(&mut debug).is_ok()
        {
            debug.unwrap().EnableDebugLayer();
        }
        let mut device_opt: Option<ID3D12Device> = None;
        D3D12CreateDevice(
            None::<&windows::core::IUnknown>,
            D3D_FEATURE_LEVEL_11_0,
            &mut device_opt,
        )
        .unwrap();
        let device = device_opt.unwrap();

        // Adapter + GPU upload heap support.
        let mut options15: windows::Win32::Graphics::Direct3D12::D3D12_FEATURE_DATA_D3D12_OPTIONS15 =
            Default::default();
        let hr = device.CheckFeatureSupport(
            windows::Win32::Graphics::Direct3D12::D3D12_FEATURE_D3D12_OPTIONS15,
            &mut options15 as *mut _ as _,
            std::mem::size_of::<
                windows::Win32::Graphics::Direct3D12::D3D12_FEATURE_DATA_D3D12_OPTIONS15,
            >() as u32,
        );
        let _ = device.CheckFeatureSupport(
            windows::Win32::Graphics::Direct3D12::D3D12_FEATURE_D3D12_OPTIONS15,
            &mut options15 as *mut _ as _,
            std::mem::size_of::<
                windows::Win32::Graphics::Direct3D12::D3D12_FEATURE_DATA_D3D12_OPTIONS15,
            >() as u32,
        );
        let _ = options15;

        let combos = [
            Combo { label: "DEFAULT+SHARED+SIMULT", heap_type: D3D12_HEAP_TYPE_DEFAULT, cpu_page: D3D12_CPU_PAGE_PROPERTY_UNKNOWN, pool: D3D12_MEMORY_POOL_UNKNOWN, flags: D3D12_RESOURCE_FLAG_ALLOW_SIMULTANEOUS_ACCESS, shared: true },
            Combo { label: "DEFAULT+SHARED+DENY", heap_type: D3D12_HEAP_TYPE_DEFAULT, cpu_page: D3D12_CPU_PAGE_PROPERTY_UNKNOWN, pool: D3D12_MEMORY_POOL_UNKNOWN, flags: D3D12_RESOURCE_FLAG_DENY_SHADER_RESOURCE, shared: true },
            Combo { label: "DEFAULT+SHARED+DENY+SIMULT", heap_type: D3D12_HEAP_TYPE_DEFAULT, cpu_page: D3D12_CPU_PAGE_PROPERTY_UNKNOWN, pool: D3D12_MEMORY_POOL_UNKNOWN, flags: windows::Win32::Graphics::Direct3D12::D3D12_RESOURCE_FLAG_DENY_SHADER_RESOURCE | windows::Win32::Graphics::Direct3D12::D3D12_RESOURCE_FLAG_ALLOW_SIMULTANEOUS_ACCESS, shared: true },
            Combo { label: "CUSTOM(WC,L0)+DENY", heap_type: D3D12_HEAP_TYPE_CUSTOM, cpu_page: D3D12_CPU_PAGE_PROPERTY_WRITE_COMBINE, pool: D3D12_MEMORY_POOL_L0, flags: D3D12_RESOURCE_FLAG_DENY_SHADER_RESOURCE, shared: false },
            Combo { label: "CUSTOM(WC,L0)+SHARED+DENY", heap_type: D3D12_HEAP_TYPE_CUSTOM, cpu_page: D3D12_CPU_PAGE_PROPERTY_WRITE_COMBINE, pool: D3D12_MEMORY_POOL_L0, flags: D3D12_RESOURCE_FLAG_DENY_SHADER_RESOURCE, shared: true },
            Combo { label: "UPLOAD+DENY", heap_type: D3D12_HEAP_TYPE_UPLOAD, cpu_page: D3D12_CPU_PAGE_PROPERTY_UNKNOWN, pool: D3D12_MEMORY_POOL_UNKNOWN, flags: D3D12_RESOURCE_FLAG_DENY_SHADER_RESOURCE, shared: false },
            Combo { label: "DEFAULT+DENY(unshared)", heap_type: D3D12_HEAP_TYPE_DEFAULT, cpu_page: D3D12_CPU_PAGE_PROPERTY_UNKNOWN, pool: D3D12_MEMORY_POOL_UNKNOWN, flags: D3D12_RESOURCE_FLAG_DENY_SHADER_RESOURCE, shared: false },
            Combo { label: "DEFAULT(noflags)", heap_type: D3D12_HEAP_TYPE_DEFAULT, cpu_page: D3D12_CPU_PAGE_PROPERTY_UNKNOWN, pool: D3D12_MEMORY_POOL_UNKNOWN, flags: windows::Win32::Graphics::Direct3D12::D3D12_RESOURCE_FLAGS(0), shared: false },
            Combo { label: "CUSTOM(WC,UNK)+DENY", heap_type: D3D12_HEAP_TYPE_CUSTOM, cpu_page: D3D12_CPU_PAGE_PROPERTY_WRITE_COMBINE, pool: D3D12_MEMORY_POOL_UNKNOWN, flags: D3D12_RESOURCE_FLAG_DENY_SHADER_RESOURCE, shared: false },
            Combo { label: "CUSTOM(UNK,L0)+DENY", heap_type: D3D12_HEAP_TYPE_CUSTOM, cpu_page: D3D12_CPU_PAGE_PROPERTY_UNKNOWN, pool: D3D12_MEMORY_POOL_L0, flags: D3D12_RESOURCE_FLAG_DENY_SHADER_RESOURCE, shared: false },
            Combo { label: "CUSTOM(WC,L1)+DENY", heap_type: D3D12_HEAP_TYPE_CUSTOM, cpu_page: D3D12_CPU_PAGE_PROPERTY_WRITE_COMBINE, pool: windows::Win32::Graphics::Direct3D12::D3D12_MEMORY_POOL_L1, flags: D3D12_RESOURCE_FLAG_DENY_SHADER_RESOURCE, shared: false },
        ];

        let (w, h) = (64u32, 64u32);
        let data = vec![0x80u8; (w * h * 4) as usize];
        for c in combos {
            let heap = D3D12_HEAP_PROPERTIES {
                Type: c.heap_type,
                CPUPageProperty: c.cpu_page,
                MemoryPoolPreference: c.pool,
                CreationNodeMask: 0,
                VisibleNodeMask: 0,
            };
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
                Flags: c.flags,
            };
            let mut resource: Option<ID3D12Resource> = None;
            let hr = device.CreateCommittedResource(
                &heap,
                if c.shared {
                    D3D12_HEAP_FLAG_SHARED
                } else {
                    windows::Win32::Graphics::Direct3D12::D3D12_HEAP_FLAGS(0)
                },
                &desc,
                D3D12_RESOURCE_STATE_COMMON,
                None,
                &mut resource,
            );
            if hr.is_err() {
                println!("{:32} create FAILED: {:?}", c.label, hr.unwrap_err().code());
                continue;
            }
            let resource = resource.unwrap();
            let wt = resource.WriteToSubresource(
                0,
                None::<*const D3D12_BOX>,
                data.as_ptr() as _,
                w * 4,
                w * 4,
            );
            let share_ok = if c.shared {
                let name = format!("probe-{}", c.label);
                let name_wide: Vec<u16> = name.encode_utf16().chain(std::iter::once(0)).collect();
                let child: ID3D12DeviceChild = resource.cast().unwrap();
                match device.CreateSharedHandle(
                    &child,
                    None,
                    GENERIC_ALL.0,
                    windows::core::PCWSTR(name_wide.as_ptr()),
                ) {
                    Ok(_) => Ok(()),
                    Err(e) => Err(format!("{e}")),
                }
            } else {
                Ok(())
            };
            println!(
                "{:32} create OK, write: {:?}, share: {:?}",
                c.label,
                wt,
                share_ok.map(|_| "ok")
            );
        }
        let _ = (CloseHandle, HANDLE::default, D3D12_BOX::default);

        // Exact engine creation order: does anything break the later share?
        {
            let (w, h) = (1200u32, 760u32);
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
            let mut res: Option<ID3D12Resource> = None;
            let hr = device.CreateCommittedResource(
                &heap,
                D3D12_HEAP_FLAG_SHARED,
                &desc,
                D3D12_RESOURCE_STATE_COMMON,
                None,
                &mut res,
            );
            println!("order: texture create {:?}", hr.map(|_| "ok"));
            let res = res.unwrap();
            let queue_desc = windows::Win32::Graphics::Direct3D12::D3D12_COMMAND_QUEUE_DESC {
                Type: D3D12_COMMAND_LIST_TYPE_DIRECT,
                ..Default::default()
            };
            let queue = device
                .CreateCommandQueue::<windows::Win32::Graphics::Direct3D12::ID3D12CommandQueue>(
                    &queue_desc,
                )
                .unwrap();
            let _ = &queue;
            let alloc = device.CreateCommandAllocator::<windows::Win32::Graphics::Direct3D12::ID3D12CommandAllocator>(D3D12_COMMAND_LIST_TYPE_DIRECT).unwrap();
            let _ = &alloc;
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
                Layout: D3D12_TEXTURE_LAYOUT_ROW_MAJOR,
                Flags: D3D12_RESOURCE_FLAGS(0),
            };
            let upload_heap = D3D12_HEAP_PROPERTIES {
                Type: D3D12_HEAP_TYPE_UPLOAD,
                CPUPageProperty: D3D12_CPU_PAGE_PROPERTY_UNKNOWN,
                MemoryPoolPreference: D3D12_MEMORY_POOL_UNKNOWN,
                CreationNodeMask: 0,
                VisibleNodeMask: 0,
            };
            let mut upload: Option<ID3D12Resource> = None;
            let hr = device.CreateCommittedResource(
                &upload_heap,
                windows::Win32::Graphics::Direct3D12::D3D12_HEAP_FLAGS(0),
                &upload_desc,
                windows::Win32::Graphics::Direct3D12::D3D12_RESOURCE_STATE_GENERIC_READ,
                None,
                &mut upload,
            );
            println!("order: upload buffer create {:?}", hr.map(|_| "ok"));
            let rfence = device
                .CreateFence::<windows::Win32::Graphics::Direct3D12::ID3D12Fence>(
                    0,
                    windows::Win32::Graphics::Direct3D12::D3D12_FENCE_FLAG_SHARED,
                )
                .unwrap();
            let ourfence = device
                .CreateFence::<windows::Win32::Graphics::Direct3D12::ID3D12Fence>(
                    0,
                    windows::Win32::Graphics::Direct3D12::D3D12_FENCE_FLAG_NONE,
                )
                .unwrap();
            let _ = &ourfence;
            // Engine also creates a command list and maps the buffer before sharing.
            let list: windows::Win32::Graphics::Direct3D12::ID3D12GraphicsCommandList = device
                .CreateCommandList(
                    0,
                    D3D12_COMMAND_LIST_TYPE_DIRECT,
                    &alloc,
                    None::<&windows::Win32::Graphics::Direct3D12::ID3D12PipelineState>,
                )
                .unwrap();
            list.Close().unwrap();
            let mut data_ptr: *mut core::ffi::c_void = std::ptr::null_mut();
            upload.unwrap().Map(0, None, Some(&mut data_ptr)).unwrap();
            let child: ID3D12DeviceChild = res.cast().unwrap();
            let luid = device.GetAdapterLuid();
            println!(
                "order: device luid {:08x}-{:08x}",
                luid.HighPart, luid.LowPart
            );
            let name_wide: Vec<u16> = "browserr-frame-1200x760"
                .encode_utf16()
                .chain(std::iter::once(0))
                .collect();
            let sh = device.CreateSharedHandle(
                &child,
                None,
                GENERIC_ALL.0,
                windows::core::PCWSTR(name_wide.as_ptr()),
            );
            println!("order: share texture {:?}", sh.map(|_| "ok"));
            let fchild: ID3D12DeviceChild = rfence.cast().unwrap();
            let fname_wide: Vec<u16> = "browserr-fence-1200x760"
                .encode_utf16()
                .chain(std::iter::once(0))
                .collect();
            let sh2 = device.CreateSharedHandle(
                &fchild,
                None,
                GENERIC_ALL.0,
                windows::core::PCWSTR(fname_wide.as_ptr()),
            );
            println!("order: share fence {:?}", sh2.map(|_| "ok"));
        }

        // Share test at the engine's real frame size.
        for (tw, th) in [(64u32, 64u32), (1200u32, 760u32), (1280u32, 760u32)] {
            let desc = D3D12_RESOURCE_DESC {
                Dimension: D3D12_RESOURCE_DIMENSION_TEXTURE2D,
                Alignment: 0,
                Width: tw as u64,
                Height: th,
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
            let mut res: Option<ID3D12Resource> = None;
            let hr = device.CreateCommittedResource(
                &heap,
                D3D12_HEAP_FLAG_SHARED,
                &desc,
                D3D12_RESOURCE_STATE_COMMON,
                None,
                &mut res,
            );
            if hr.is_err() {
                println!(
                    "share test {tw}x{th}: create failed {:?}",
                    hr.unwrap_err().code()
                );
                continue;
            }
            let res = res.unwrap();
            let child: ID3D12DeviceChild = res.cast().unwrap();
            let name_wide: Vec<u16> = format!("probe-share-{tw}x{th}")
                .encode_utf16()
                .chain(std::iter::once(0))
                .collect();
            let sh = device.CreateSharedHandle(
                &child,
                None,
                GENERIC_ALL.0,
                windows::core::PCWSTR(name_wide.as_ptr()),
            );
            println!("share test {tw}x{th}: share {:?}", sh.map(|_| "ok"));
        }

        // Plain upload buffer, the standard CPU-upload staging pattern.
        let upload_desc = D3D12_RESOURCE_DESC {
            Dimension: D3D12_RESOURCE_DIMENSION_BUFFER,
            Alignment: 0,
            Width: 65536,
            Height: 1,
            DepthOrArraySize: 1,
            MipLevels: 1,
            Format: Default::default(),
            SampleDesc: DXGI_SAMPLE_DESC {
                Count: 1,
                Quality: 0,
            },
            Layout: D3D12_TEXTURE_LAYOUT_ROW_MAJOR,
            Flags: D3D12_RESOURCE_FLAGS(0),
        };
        let upload_heap = D3D12_HEAP_PROPERTIES {
            Type: D3D12_HEAP_TYPE_UPLOAD,
            CPUPageProperty: D3D12_CPU_PAGE_PROPERTY_UNKNOWN,
            MemoryPoolPreference: D3D12_MEMORY_POOL_UNKNOWN,
            CreationNodeMask: 0,
            VisibleNodeMask: 0,
        };
        let mut buf: Option<ID3D12Resource> = None;
        let hr = device.CreateCommittedResource(
            &upload_heap,
            windows::Win32::Graphics::Direct3D12::D3D12_HEAP_FLAGS(0),
            &upload_desc,
            windows::Win32::Graphics::Direct3D12::D3D12_RESOURCE_STATE_GENERIC_READ,
            None,
            &mut buf,
        );
        println!("upload buffer: {:?}, resource: {}", hr, buf.is_some());

        // Which adapter did we get, and does an upload buffer work per adapter?
        use windows::Win32::Graphics::Dxgi::{
            CreateDXGIFactory1, DXGI_ADAPTER_FLAG_SOFTWARE, IDXGIAdapter1, IDXGIFactory1,
        };
        let factory: IDXGIFactory1 = CreateDXGIFactory1().unwrap();
        let mut i = 0u32;
        while let Ok(adapter) = factory.EnumAdapters1(i) {
            let desc = adapter.GetDesc1().unwrap();
            let name = String::from_utf16_lossy(&desc.Description);
            let mut dev: Option<ID3D12Device> = None;
            let ok = D3D12CreateDevice(&adapter, D3D_FEATURE_LEVEL_11_0, &mut dev).is_ok();
            let sw = (desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE.0 as u32) != 0;
            println!("adapter {i}: {name} sw={sw} d3d12={ok}");
            if ok {
                let dev = dev.unwrap();
                let mut b: Option<ID3D12Resource> = None;
                let hr2 = dev.CreateCommittedResource(
                    &upload_heap,
                    windows::Win32::Graphics::Direct3D12::D3D12_HEAP_FLAGS(0),
                    &upload_desc,
                    windows::Win32::Graphics::Direct3D12::D3D12_RESOURCE_STATE_GENERIC_READ,
                    None,
                    &mut b,
                );
                println!(
                    "  upload buffer on this adapter: {:?}, ok={}",
                    hr2,
                    b.is_some()
                );
            }
            i += 1;
            if i > 5 {
                break;
            }
        }
        if let Ok(info) = device.cast::<windows::Win32::Graphics::Direct3D12::ID3D12InfoQueue>() {
            let n = info.GetNumStoredMessages();
            for i in 0..n {
                let mut size = 0usize;
                let _ = info.GetMessage(i, None, &mut size);
                let mut bufv = vec![0u8; size];
                let _ = info.GetMessage(i, Some(bufv.as_mut_ptr() as _), &mut size);
                let msg = unsafe {
                    &*(bufv.as_ptr() as *const windows::Win32::Graphics::Direct3D12::D3D12_MESSAGE)
                };
                let desc = unsafe { std::ffi::CStr::from_ptr(msg.pDescription as _) };
                println!("d3d12: {}", desc.to_string_lossy());
            }
        }
        let _ = |_: &ID3D12Fence| {};
        let _ = D3D12_RESOURCE_STATES(0);
    }
}
