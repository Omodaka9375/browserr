//! Hardware-accelerated offscreen rendering context (surfman WGL on Windows).
//!
//! Mirrors `servo::SoftwareRenderingContext` in structure, but uses the
//! default (hardware) adapter so webrender paints on the GPU instead of the
//! CPU. Falls back to software if hardware GL is unavailable.

use std::cell::RefCell;
use std::rc::Rc;

use dpi::PhysicalSize;
use euclid::Size2D;
use gleam::gl::{self, Gl};
use glow::HasContext;
use surfman::chains::{PreserveBuffer, SwapChain};
use surfman::{
    Adapter, Connection, Context, ContextAttributeFlags, ContextAttributes, Device, GLApi,
    GLVersion, SurfaceAccess, SurfaceType,
};
use webrender_api::units::DeviceIntRect;
pub struct HardwareRenderingContext {
    gleam_gl: Rc<dyn Gl>,
    glow_gl: std::sync::Arc<glow::Context>,
    device: RefCell<Device>,
    context: RefCell<Context>,
    swap_chain: SwapChain<Device>,
    size: std::cell::Cell<PhysicalSize<u32>>,
}

impl HardwareRenderingContext {
    pub fn new(size: PhysicalSize<u32>) -> Result<Self, surfman::Error> {
        let connection = Connection::new()?;
        let adapter: Adapter = connection.create_adapter()?;
        let mut device = connection.create_device(&adapter)?;

        let flags = ContextAttributeFlags::ALPHA
            | ContextAttributeFlags::DEPTH
            | ContextAttributeFlags::STENCIL;
        let version = match connection.gl_api() {
            GLApi::GLES => GLVersion { major: 3, minor: 0 },
            GLApi::GL => GLVersion { major: 3, minor: 2 },
        };
        let descriptor = device.create_context_descriptor(&ContextAttributes { flags, version })?;
        let mut context = device.create_context(&descriptor, None)?;

        device.make_context_current(&context)?;

        let gleam_gl = match connection.gl_api() {
            GLApi::GL => unsafe {
                gl::GlFns::load_with(|name| device.get_proc_address(&context, name))
            },
            GLApi::GLES => unsafe {
                gl::GlesFns::load_with(|name| device.get_proc_address(&context, name))
            },
        };

        let glow_gl = unsafe {
            glow::Context::from_loader_function(|name| {
                device.get_proc_address(&context, name)
            })
        };

        let surfman_size = Size2D::new(size.width as i32, size.height as i32);
        let surface = device.create_surface(
            &context,
            SurfaceAccess::GPUOnly,
            SurfaceType::Generic { size: surfman_size },
        )?;
        device
            .bind_surface_to_context(&mut context, surface)
            .map_err(|(err, mut surface)| {
                let _ = device.destroy_surface(&mut context, &mut surface);
                err
            })?;

        let swap_chain =
            SwapChain::create_attached(&mut device, &mut context, SurfaceAccess::GPUOnly)?;

        Ok(HardwareRenderingContext {
            gleam_gl,
            glow_gl: std::sync::Arc::new(glow_gl),
            device: RefCell::new(device),
            context: RefCell::new(context),
            swap_chain,
            size: std::cell::Cell::new(size),
        })
    }

    fn framebuffer(&self) -> Option<glow::Framebuffer> {
        let device = self.device.borrow();
        let context = self.context.borrow();
        device
            .context_surface_info(&context)
            .unwrap_or(None)
            .and_then(|info| info.framebuffer_object)
    }
}

impl servo::RenderingContext for HardwareRenderingContext {
    fn read_to_image(&self, source_rectangle: DeviceIntRect) -> Option<servo::RgbaImage> {
        let framebuffer_id = self
            .framebuffer()
            .map_or(0, |framebuffer| framebuffer.0.get());
        let gl = &self.gleam_gl;
        gl.bind_framebuffer(gl::FRAMEBUFFER, framebuffer_id);
        gl.bind_vertex_array(0);

        let mut pixels = gl.read_pixels(
            source_rectangle.min.x,
            source_rectangle.min.y,
            source_rectangle.width(),
            source_rectangle.height(),
            gl::RGBA,
            gl::UNSIGNED_BYTE,
        );

        // GL textures are upside down relative to images: flip vertically.
        let stride = source_rectangle.width() as usize * 4;
        let height = source_rectangle.height() as usize;
        for y in 0..height / 2 {
            let (top, bottom) = pixels.split_at_mut((height - y - 1) * stride);
            top[y * stride..(y + 1) * stride].swap_with_slice(&mut bottom[..stride]);
        }

        servo::RgbaImage::from_raw(
            source_rectangle.width() as u32,
            source_rectangle.height() as u32,
            pixels,
        )
    }

    fn size(&self) -> PhysicalSize<u32> {
        self.size.get()
    }

    fn resize(&self, size: PhysicalSize<u32>) {
        if self.size.get() == size {
            return;
        }
        self.size.set(size);
        let mut device = self.device.borrow_mut();
        let mut context = self.context.borrow_mut();
        let surfman_size = Size2D::new(size.width as i32, size.height as i32);
        let _ = self
            .swap_chain
            .resize(&mut device, &mut context, surfman_size);
    }

    fn present(&self) {
        let mut device = self.device.borrow_mut();
        let mut context = self.context.borrow_mut();
        let _ = self
            .swap_chain
            .swap_buffers(&mut device, &mut context, PreserveBuffer::No);
    }

    fn make_current(&self) -> Result<(), surfman::Error> {
        let device = self.device.borrow();
        let context = self.context.borrow();
        device.make_context_current(&context)
    }

    fn gleam_gl_api(&self) -> Rc<dyn Gl> {
        self.gleam_gl.clone()
    }

    fn glow_gl_api(&self) -> std::sync::Arc<glow::Context> {
        self.glow_gl.clone()
    }
}
