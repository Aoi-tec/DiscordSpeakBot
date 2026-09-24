use anyhow::{bail, Context, Result};
use std::ffi::c_void;
use std::os::windows::io::AsRawHandle;
use std::process::Child;
use windows_sys::Win32::{
    Foundation::{CloseHandle, GetLastError, ERROR_ALREADY_EXISTS, HANDLE},
    Security::Credentials::{
        CredFree, CredReadW, CredWriteW, CREDENTIALW, CRED_PERSIST_LOCAL_MACHINE, CRED_TYPE_GENERIC,
    },
    System::{
        JobObjects::{
            AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
            SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
        },
        Threading::{CreateMutexW, SetPriorityClass, BELOW_NORMAL_PRIORITY_CLASS},
    },
};

fn wide(value: &str) -> Vec<u16> {
    value.encode_utf16().chain(Some(0)).collect()
}

pub struct OwnedHandle(pub HANDLE);
unsafe impl Send for OwnedHandle {}
impl Drop for OwnedHandle {
    fn drop(&mut self) {
        unsafe {
            CloseHandle(self.0);
        }
    }
}

pub fn single_instance() -> Result<OwnedHandle> {
    let user = std::env::var("USERNAME").unwrap_or_default();
    let name = wide(&format!("Local\\DiscordSpeakBot-{user}"));
    let handle = unsafe { CreateMutexW(std::ptr::null(), 0, name.as_ptr()) };
    if handle.is_null() {
        bail!("Cannot create Host mutex");
    }
    let guard = OwnedHandle(handle);
    if unsafe { GetLastError() } == ERROR_ALREADY_EXISTS {
        bail!("Host is already running");
    }
    Ok(guard)
}

pub fn child_job(child: &Child, below_normal: bool) -> Result<OwnedHandle> {
    unsafe {
        let handle = CreateJobObjectW(std::ptr::null(), std::ptr::null());
        if handle.is_null() {
            bail!("CreateJobObject failed");
        }
        let job = OwnedHandle(handle);
        let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        if SetInformationJobObject(
            handle,
            JobObjectExtendedLimitInformation,
            &info as *const _ as *const c_void,
            std::mem::size_of_val(&info) as u32,
        ) == 0
        {
            bail!("SetInformationJobObject failed");
        }
        let process = child.as_raw_handle() as HANDLE;
        if AssignProcessToJobObject(handle, process) == 0 {
            bail!("AssignProcessToJobObject failed");
        }
        if below_normal && SetPriorityClass(process, BELOW_NORMAL_PRIORITY_CLASS) == 0 {
            bail!("SetPriorityClass failed");
        }
        Ok(job)
    }
}

pub fn read_token() -> Result<String> {
    let target = wide("DiscordSpeakBot/DiscordToken");
    unsafe {
        let mut credential: *mut CREDENTIALW = std::ptr::null_mut();
        if CredReadW(target.as_ptr(), CRED_TYPE_GENERIC, 0, &mut credential) == 0 {
            return Ok(String::new());
        }
        let bytes = std::slice::from_raw_parts(
            (*credential).CredentialBlob,
            (*credential).CredentialBlobSize as usize,
        );
        let result = String::from_utf8(bytes.to_vec()).context("Credential is not UTF-8");
        CredFree(credential as *const c_void);
        result
    }
}

pub fn write_token(token: &str) -> Result<()> {
    if token.len() > 4096 {
        bail!("Token is too long");
    }
    let mut target = wide("DiscordSpeakBot/DiscordToken");
    let mut username = wide("DiscordBot");
    let mut bytes = token.as_bytes().to_vec();
    unsafe {
        let mut credential: CREDENTIALW = std::mem::zeroed();
        credential.Type = CRED_TYPE_GENERIC;
        credential.TargetName = target.as_mut_ptr();
        credential.UserName = username.as_mut_ptr();
        credential.CredentialBlob = bytes.as_mut_ptr();
        credential.CredentialBlobSize = bytes.len() as u32;
        credential.Persist = CRED_PERSIST_LOCAL_MACHINE;
        let ok = CredWriteW(&credential, 0);
        bytes.fill(0);
        if ok == 0 {
            bail!("Cannot save Credential Manager token");
        }
    }
    Ok(())
}

pub fn cpu_sets(child: &Child, mode: &str, custom: &[u32]) -> Result<Vec<u32>> {
    use windows_sys::Win32::System::{
        SystemInformation::{
            CpuSetInformation, GetSystemCpuSetInformation, SYSTEM_CPU_SET_INFORMATION,
        },
        Threading::SetProcessDefaultCpuSets,
    };
    if mode == "automatic" {
        return Ok(vec![]);
    }
    unsafe {
        let process = child.as_raw_handle() as HANDLE;
        let mut size = 0;
        GetSystemCpuSetInformation(std::ptr::null_mut(), 0, &mut size, process, 0);
        if size == 0 {
            bail!("CPU topology unavailable");
        }
        let mut buffer = vec![0u8; size as usize];
        if GetSystemCpuSetInformation(buffer.as_mut_ptr() as *mut _, size, &mut size, process, 0)
            == 0
        {
            bail!("CPU topology query failed");
        }
        let mut cores = std::collections::BTreeMap::<(u16, u8), Vec<u32>>::new();
        let mut offset = 0;
        while offset + std::mem::size_of::<SYSTEM_CPU_SET_INFORMATION>() <= buffer.len() {
            let info = std::ptr::read_unaligned(
                buffer.as_ptr().add(offset) as *const SYSTEM_CPU_SET_INFORMATION
            );
            if info.Size < 8 || offset + info.Size as usize > buffer.len() {
                break;
            }
            if info.Type == CpuSetInformation {
                let cpu = info.Anonymous.CpuSet;
                let flags = cpu.Anonymous1.AllFlags;
                if flags & 1 == 0 && (flags & 2 == 0 || flags & 4 != 0) {
                    cores
                        .entry((cpu.Group, cpu.CoreIndex))
                        .or_default()
                        .push(cpu.Id);
                }
            }
            offset += info.Size as usize;
        }
        let ids = if mode == "low_impact" {
            cores
                .values()
                .rev()
                .find(|ids| ids.len() >= 2)
                .or_else(|| cores.values().next_back())
                .cloned()
                .context("No eligible CPU core")?
        } else {
            if custom.is_empty()
                || custom
                    .iter()
                    .any(|id| !cores.values().any(|ids| ids.contains(id)))
            {
                bail!("Invalid custom CPU Set IDs");
            }
            custom.to_vec()
        };
        if SetProcessDefaultCpuSets(process, ids.as_ptr(), ids.len() as u32) == 0 {
            bail!("CPU Sets could not be applied; using Automatic");
        }
        Ok(ids)
    }
}

pub fn play_wav(path: &std::path::Path) -> Result<()> {
    use windows_sys::Win32::Media::Audio::{PlaySoundW, SND_ASYNC, SND_FILENAME, SND_NODEFAULT};
    let name = wide(&path.to_string_lossy());
    if unsafe {
        PlaySoundW(
            name.as_ptr(),
            std::ptr::null_mut(),
            SND_ASYNC | SND_FILENAME | SND_NODEFAULT,
        )
    } == 0
    {
        bail!("Audio playback failed");
    }
    Ok(())
}
