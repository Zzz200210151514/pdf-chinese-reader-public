"""Save a MinerU token to the current Windows user's Credential Manager."""
import ctypes,getpass,sys
from ctypes import wintypes

class CREDENTIAL(ctypes.Structure):
 _fields_=[('Flags',wintypes.DWORD),('Type',wintypes.DWORD),('TargetName',wintypes.LPWSTR),
  ('Comment',wintypes.LPWSTR),('LastWritten',wintypes.FILETIME),('CredentialBlobSize',wintypes.DWORD),
  ('CredentialBlob',ctypes.POINTER(ctypes.c_ubyte)),('Persist',wintypes.DWORD),('AttributeCount',wintypes.DWORD),
  ('Attributes',ctypes.c_void_p),('TargetAlias',wintypes.LPWSTR),('UserName',wintypes.LPWSTR)]

def save(token):
 if sys.platform!='win32':raise OSError('This helper stores credentials in Windows Credential Manager.')
 data=token.encode('utf-8')
 if not data or len(data)>512:raise ValueError('Empty or oversized credential.')
 blob=(ctypes.c_ubyte*len(data)).from_buffer_copy(data)
 c=CREDENTIAL(0,1,'pdf-chinese-reader/MinerU',None,wintypes.FILETIME(0,0),len(data),blob,2,0,None,None,'MinerU API token')
 api=ctypes.WinDLL('Advapi32.dll',use_last_error=True);api.CredWriteW.argtypes=[ctypes.POINTER(CREDENTIAL),wintypes.DWORD];api.CredWriteW.restype=wintypes.BOOL
 if not api.CredWriteW(ctypes.byref(c),0):raise ctypes.WinError(ctypes.get_last_error())

if __name__=='__main__':
 token=getpass.getpass('MinerU API key (hidden; saved to Windows Credential Manager): ')
 try:save(token);print('Saved for this Windows user. The key was not printed.')
 finally:token=''
