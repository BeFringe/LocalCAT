#ifndef LOCALCAT_FROZEN_ENTRY_H
#define LOCALCAT_FROZEN_ENTRY_H

/* Called only by the release-owned Windows wWinMain. No argv/environment
 * attestation or stock PyInstaller/PYZ fallback is accepted. */
int localcat_frozen_entry(void);

#endif
