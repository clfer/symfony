<?php
// Simulates Windows: time() enters the next second a few ms before new file mtimes do.
// Usage: copy into <8.2 worktree>/var/, then: WD=$PWD ../flaky-kb/php84.sh php var/config-cache-clock-skew.php
// Expected: skew=0 -> fresh=true after 2nd build; skew=1 -> fresh=false (the CI failure of fix #3).
namespace Symfony\Component\Config { function time(): int { return \time() + $GLOBALS['clockSkew']; } }
namespace {
require __DIR__.'/../vendor/autoload.php';
use Symfony\Component\Config\Resource\FileResource;
use Symfony\Component\Config\Resource\SelfCheckingResourceChecker;
use Symfony\Component\Config\ResourceCheckerConfigCache;
foreach ([0, 1] as $skew) {
    $GLOBALS['clockSkew'] = 0;
    $file = tempnam(sys_get_temp_dir(), 'c'); $res = tempnam(sys_get_temp_dir(), 'r');
    $cache = new ResourceCheckerConfigCache($file, [new SelfCheckingResourceChecker()]);
    sleep(1); file_put_contents($res, 'generated'); clearstatcache();
    $cache->write('first', [new FileResource($res)]); clearstatcache();
    $first = $cache->isFresh();
    $GLOBALS['clockSkew'] = $skew; // the second rolls over for time() only, between isFresh() and write()
    $cache->write('second', [new FileResource($res)]); clearstatcache();
    printf("skew=%d: after 1st build fresh=%s, after 2nd build fresh=%s (resource mtime %d, meta mtime %d)\n", $skew, var_export($first, true), var_export($cache->isFresh(), true), filemtime($res), filemtime($file.'.meta'));
    @unlink($file); @unlink($file.'.meta'); @unlink($file.'.meta.json'); @unlink($res);
}
}
