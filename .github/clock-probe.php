<?php
// Measures whether the filesystem clock lags PHP's time(): write a file right
// after reading time() and count how often its mtime is an earlier second.
$f = sys_get_temp_dir().'/clock_probe';
$n = 20000; $behind = 0; $ahead = 0; $maxLagUs = 0;
for ($i = 0; $i < $n; ++$i) {
    $before = time();
    $us0 = microtime(true);
    file_put_contents($f, 'x');
    clearstatcache();
    $m = filemtime($f);
    $after = time();
    if ($m < $before) { ++$behind; }
    if ($m > $after) { ++$ahead; }
    if ($m < $before) { $maxLagUs = max($maxLagUs, (int) (($us0 - $before) * 1e6)); }
    usleep(random_int(0, 3000));
}
unlink($f);
printf("CLOCK PROBE: %d samples, mtime < time() before write: %d, mtime > time() after write: %d, max ms into new second while mtime still old: %.1f\n", $n, $behind, $ahead, $maxLagUs / 1000);
