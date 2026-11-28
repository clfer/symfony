<?php

/*
 * This file is part of the Symfony package.
 *
 * (c) Fabien Potencier <fabien@symfony.com>
 *
 * For the full copyright and license information, please view the LICENSE
 * file that was distributed with this source code.
 */

namespace Symfony\Contracts\HttpClient\Test;

use Symfony\Component\Process\PhpExecutableFinder;
use Symfony\Component\Process\Process;

class TestHttpServer
{
    private static array $process = [];

    /**
     * @param int         $port             Defaults to the SYMFONY_HTTP_CLIENT_TEST_PORT env var, or 8057
     * @param string|null $workingDirectory
     */
    public static function start(int $port = 8057/* , ?string $workingDirectory = null */): Process
    {
        $port = \func_num_args() ? $port : self::getDefaultPort();
        $workingDirectory = \func_get_args()[1] ?? __DIR__.'/Fixtures/web';

        if (0 > $port) {
            $port = -$port;
            $ip = '[::1]';
        } else {
            $ip = '127.0.0.1';
        }

        if (isset(self::$process[$port])) {
            self::$process[$port]->stop();
        } else {
            register_shutdown_function(static function () use ($port) {
                self::$process[$port]->stop();
            });
        }

        $finder = new PhpExecutableFinder();
        $process = new Process(array_merge([$finder->find(false)], $finder->findArguments(), ['-dopcache.enable=0', '-dvariables_order=EGPCS', '-S', $ip.':'.$port]));
        $process->setWorkingDirectory($workingDirectory);
        $process->start();
        self::$process[$port] = $process;

        $t0 = microtime(true);
        $next = 5;
        do {
            usleep(50000);
            if (microtime(true) - $t0 > $next) {
                $next += 5;
                fwrite(\STDERR, \sprintf("\n[HANGDBG] start(%d) spinning %.1fs running=%s exit=%s out=%s err=%s\n", $port, microtime(true) - $t0, var_export($process->isRunning(), true), var_export($process->getExitCode(), true), $process->getOutput(), $process->getErrorOutput()));
            }
        } while (!@fopen('http://'.$ip.':'.$port, 'r'));

        return $process;
    }

    public static function stop(int $port = 8057)
    {
        $port = \func_num_args() ? $port : self::getDefaultPort();

        if (isset(self::$process[$port])) {
            self::$process[$port]->stop();
        }
    }

    private static function getDefaultPort(): int
    {
        return (int) (getenv('SYMFONY_HTTP_CLIENT_TEST_PORT') ?: 8057);
    }
}
