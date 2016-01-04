# Installation

This tool uses virtualenv and python 2.7. Please do:

    $ mkvirtualenv exazk
    $ workon exazk
    $ pip install -r requirements.txt

# Run

## testing

Running exazk outside of exabgp (for testing purpose):

    ./exazk.py -f conf/apex-node1.yml

is equivalent to:

    ./exazk.py -sF daemon -c /usr/local/bin/check_local_nginx.sh -n apex -A 10.20.255.1 -N 10.20.255.2 -N 10.20.255.3 -zH localhost -zPS /exabgp/service/apex -zPM /exabgp/maintenance/apex

## inside ExaBGP

Running exazk inside exabgp:

    process exazk-apex {
        run /Users/remi/github/exazk/exazk.py -sF daemon -c /usr/local/bin/check_local_nginx.sh -n apex -A 10.20.255.1 -N 10.20.255.2 -N 10.20.255.3 -zH localhost -zPS /exabgp/service/apex -zPM /exabgp/maintenance/apex;
    }

