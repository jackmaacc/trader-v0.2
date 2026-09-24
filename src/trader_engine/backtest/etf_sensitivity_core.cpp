// C++17 offline sensitivity kernel. Mirrors BacktestEngine.run_etf_replay ordering.
// No network, broker, global mutable state, or random generation. Build without
// fast-math and with -ffp-contract=off to preserve sequential floating arithmetic.
#include <algorithm>
#include <array>
#include <cmath>
#include <limits>
#include <vector>

namespace {
struct Position {
    bool active=false;
    double qty=0,basis=0,raw_basis=0,entry_fee=0,dividends=0,stop=0,target=0;
    int entry_day=0;
    long long entry_minute=0;
};
struct Pending { bool active=false; long long due=0; double score=0,atr=0,target=0; };
struct Claim { double amount; int pay; };
struct Candidate { int symbol; double score,atr,target; };
struct CapacityFailure {};
constexpr int alphabet[5]={3,2,1,4,0}; // GLD,IWM,QQQ,SPY,TLT
}

extern "C" int etf_scenario(
    int T,int D,int A,int family,int horizon,int delay,double impact,double commission,
    const double* limits,const long long* epoch_minutes,const int* day,const int* offset,
    const int* day_lengths,const double* prices,const double* mr,const double* mom,
    const int* action_ex,const int* action_pay,const int* action_symbol,
    const double* action_values,double* daily,double* summary,double* events,int max_events) {
    if(T<=0||D<=0||A<0||(family!=0&&family!=1)||horizon<=0||delay<0||delay>60||
       !std::isfinite(impact)||!std::isfinite(commission)||impact<0||impact>=1||commission<0||commission>=1||
       !limits||!epoch_minutes||!day||!offset||!day_lengths||!prices||!mr||!mom||!daily||!summary||max_events<0||
       (max_events>0&&!events)||(A>0&&(!action_ex||!action_pay||!action_symbol||!action_values)))return -1;
    try {
        std::fill(daily,daily+D*12,0.);std::fill(summary,summary+16,0.);
        std::array<Position,5> pos{};
        std::array<Pending,5> pending{};
        std::array<bool,5> entered{};
        std::array<double,5> marks{},realized{},gross_realized{};
        std::vector<int> order;order.reserve(5);
        std::vector<Claim> claims;claims.reserve(A);
        double cash=100000.,fees=0.,impact_cost=0.,paid=0.,peak=100000.,previous_close=100000.;
        double close_peak=100000.,max_close_dd=0.;
        bool persistent_halt=false,daily_halt=false,data_complete=true,valuation_valid=true;
        int events_count=0,closed_count=0,entries=0,halt_days=0,action_cursor=0,current_day=-1;
        auto receivable=[&](){double v=0.;for(const auto& c:claims)v+=c.amount;return v;};
        auto equity=[&](){double v=0.;for(int s:order)v+=pos[s].qty*marks[s];return cash+v+receivable();};
        auto halt_check=[&](){double e=equity();peak=std::max(peak,e);
            if(e<=peak*(1-limits[7]))persistent_halt=true;
            if(e<=previous_close*(1-limits[6]))daily_halt=true;};
        auto record=[&](int t,int s,int side,int reason,double qty,double raw,double fill,double fee,double net,double gross){
            if(events_count>=max_events)throw CapacityFailure{};
            double* r=events+10*events_count++;
            r[0]=t;r[1]=s;r[2]=side;r[3]=reason;r[4]=qty;r[5]=raw;r[6]=fill;r[7]=fee;r[8]=net;r[9]=gross;
        };
        auto close=[&](int s,int t,double raw,int reason){
            Position p=pos[s];pos[s].active=false;entered[s]=true;
            order.erase(std::find(order.begin(),order.end(),s));
            double fill=raw*(1-impact),fee=p.qty*fill*commission;
            cash+=p.qty*fill-fee;fees+=fee;impact_cost+=p.qty*(raw-fill);
            double net=p.qty*fill-fee-p.basis+p.dividends;
            double gross=p.qty*raw-p.raw_basis+p.dividends;
            realized[s]+=net;gross_realized[s]+=gross;++closed_count;
            record(t,s,-1,reason,p.qty,raw,fill,fee,net,gross);
        };
        for(int t=0;t<T;++t){
            int d=day[t],minute=offset[t];
            if(d<0||d>=D||minute<0||minute>=day_lengths[d]||(t&&epoch_minutes[t]<=epoch_minutes[t-1]))return -1;
            if(d!=current_day){current_day=d;daily_halt=false;entered.fill(false);pending.fill(Pending{});}
            int cutoff=day_lengths[d]-5,execution_start=5+delay;
            std::array<bool,5> present{};
            for(int s=0;s<5;++s){present[s]=std::isfinite(prices[(t*5+s)*4]);if(!present[s])data_complete=false;}
            for(int s:order)if(!present[s])valuation_valid=false;
            while(action_cursor<A&&action_ex[action_cursor]<=t){
                int a=action_cursor++,s=action_symbol[a];
                double ratio=action_values[2*a],div=action_values[2*a+1];
                if(s<0||s>=5||!std::isfinite(ratio)||ratio<=0||!std::isfinite(div)||div<0||
                   (action_pay[a]>=0&&action_pay[a]<action_ex[a]))return -1;
                if(pos[s].active){
                    auto& p=pos[s];p.qty*=ratio;p.stop/=ratio;
                    if(family==0)p.target/=ratio;
                    marks[s]/=ratio;
                    double credit=p.qty*div;p.dividends+=credit;
                    if(credit!=0.)claims.push_back({credit,action_pay[a]});
                }
            }
            // Preserve claim insertion order through settlement, independent of ownership.
            auto write=claims.begin();
            for(auto it=claims.begin();it!=claims.end();++it){
                if(it->pay>=0&&it->pay<=t){cash+=it->amount;paid+=it->amount;}
                else {*write=*it;++write;}
            }
            claims.erase(write,claims.end());
            for(int s=0;s<5;++s)if(present[s])marks[s]=prices[(t*5+s)*4];
            halt_check();
            std::array<int,5> exits{};int exit_count=static_cast<int>(order.size());
            std::copy(order.begin(),order.end(),exits.begin());
            for(int exit_i=0;exit_i<exit_count;++exit_i){int s=exits[exit_i];
                auto& p=pos[s];if(!present[s])continue;
                double raw=prices[(t*5+s)*4];int reason=-1;
                if(raw<=p.stop)reason=1;
                else if(persistent_halt||daily_halt)reason=persistent_halt?2:3;
                else if(family==0&&(minute>=cutoff||epoch_minutes[t]-p.entry_minute>=horizon))reason=minute>=cutoff?4:5;
                else if(family==1&&(d-p.entry_day>=5||(minute>=cutoff&&d-p.entry_day>=4)))reason=6;
                else if(family==1&&minute>=execution_start&&mom[(d*5+s)*4+3]!=0.)reason=7;
                if(reason>=0)close(s,t,raw,reason);
            }
            halt_check();
            std::array<Candidate,5> candidates{};int candidate_count=0;
            if(family==1&&minute>=execution_start&&minute<cutoff){
                for(int s=0;s<5;++s){const double* sig=mom+(d*5+s)*4;
                    if(sig[0]!=0.&&!pos[s].active&&!entered[s]&&present[s])candidates[candidate_count++]={s,sig[1],sig[2],0.};}
            }else if(family==0){
                for(int s=0;s<5;++s){auto p=pending[s];if(!p.active||epoch_minutes[t]<p.due)continue;
                    pending[s].active=false;
                    if(epoch_minutes[t]!=p.due||!present[s])continue;
                    if(delay&&(t==0||day[t-1]!=d||epoch_minutes[t-1]!=epoch_minutes[t]-1||!std::isfinite(mr[((t-1)*5+s)*3])))continue;
                    candidates[candidate_count++]={s,p.score,p.atr,p.target};}
            }
            std::sort(candidates.begin(),candidates.begin()+candidate_count,[](const Candidate& a,const Candidate& b){
                return a.score>b.score||(a.score==b.score&&alphabet[a.symbol]<alphabet[b.symbol]);});
            for(int candidate_i=0;candidate_i<candidate_count;++candidate_i){const auto& c=candidates[candidate_i];
                int s=c.symbol;if(pos[s].active||entered[s]||persistent_halt||daily_halt||minute>=cutoff)continue;
                bool stale=false;for(int k:order)if(!present[k])stale=true;if(stale)continue;
                double raw=prices[(t*5+s)*4],fill=raw*(1+impact),distance=(family==0?1.5:3.)*c.atr;
                if(distance<=0||distance>=fill||(family==0&&c.target<=fill))continue;
                double eq=equity(),gross=0.,risk=0.,cluster=0.;
                for(int k:order){const auto& p=pos[k];gross+=p.qty*marks[k];
                    risk+=p.qty*std::max(marks[k]-std::min(marks[k],p.stop)*(1-impact)*(1-commission),0.);
                    if(k<3)cluster+=p.qty*marks[k];}
                double cap=family==1?std::min(limits[2],limits[5]):limits[2];
                double per_cost=fill*(1+commission)-raw;
                // Entry impact can place the new stop above the current raw open.
                // Budget and execute that immediately marketable stop at the open.
                double sizing_stop=std::min(fill-distance,raw);
                double stop_cost=fill*(1+commission)-sizing_stop*(1-impact)*(1-commission);
                double qty=cash/(fill*(1+commission));
                qty=std::min(qty,limits[0]*eq/stop_cost);
                qty=std::min(qty,std::max(0.,limits[1]*eq-risk)/(stop_cost+limits[1]*per_cost));
                qty=std::min(qty,std::max(0.,cap*eq-gross)/(raw+cap*per_cost));
                qty=std::min(qty,limits[3]*eq/(raw+limits[3]*per_cost));
                if(s<3)qty=std::min(qty,std::max(0.,limits[4]*eq-cluster)/(raw+limits[4]*per_cost));
                qty=std::max(0.,std::floor(qty));if(qty<1||order.size()>=5)continue;
                double fee=qty*fill*commission,basis=qty*fill+fee;
                cash-=basis;fees+=fee;impact_cost+=qty*(fill-raw);
                Position p;p.active=true;p.qty=qty;p.basis=basis;p.raw_basis=qty*raw;p.entry_fee=fee;
                p.stop=fill-distance;p.target=c.target;p.entry_day=d;p.entry_minute=epoch_minutes[t];
                pos[s]=p;order.push_back(s);entered[s]=true;++entries;
                record(t,s,1,0,qty,raw,fill,fee,0.,0.);
            }
            exit_count=static_cast<int>(order.size());std::copy(order.begin(),order.end(),exits.begin());
            for(int exit_i=0;exit_i<exit_count;++exit_i){int s=exits[exit_i];if(!present[s])continue;auto& p=pos[s];const double* r=prices+(t*5+s)*4;
                if(r[2]<=p.stop)close(s,t,std::min(r[0],p.stop),8);
                else if(family==0&&r[1]>=p.target)close(s,t,p.target,9);}
            for(int s=0;s<5;++s)if(present[s])marks[s]=prices[(t*5+s)*4+3];
            halt_check();
            double eq=equity();close_peak=std::max(close_peak,eq);max_close_dd=std::max(max_close_dd,1-eq/close_peak);
            if(family==0&&minute+1<cutoff){
                for(int s=0;s<5;++s){const double* sig=mr+(t*5+s)*3;
                    if(present[s]&&!entered[s]&&!pos[s].active&&!pending[s].active&&std::isfinite(sig[0]))
                        pending[s]={true,epoch_minutes[t]+1+delay,sig[0],sig[1],sig[2]};}
            }
            if(t==T-1||day[t+1]!=d){
                double* r=daily+d*12;double net=eq-100000.;
                r[0]=eq;r[1]=cash;r[2]=net+fees+impact_cost;r[3]=net;r[4]=fees;r[5]=impact_cost;
                r[6]=receivable();r[7]=paid;r[8]=closed_count;r[9]=order.size();r[10]=daily_halt;r[11]=persistent_halt;
                previous_close=eq;if(daily_halt)++halt_days;
            }
        }
        double net=0.,gross=0.;
        for(int s=0;s<5;++s){double un=0.,ug=0.;if(pos[s].active){const auto& p=pos[s];
            un=p.qty*marks[s]-p.basis+p.dividends;ug=p.qty*marks[s]-p.raw_basis+p.dividends;}
            net+=realized[s]+un;gross+=gross_realized[s]+ug;}
        summary[0]=equity();summary[1]=cash;summary[2]=gross;summary[3]=net;summary[4]=fees;summary[5]=impact_cost;
        summary[6]=receivable();summary[7]=paid;summary[8]=closed_count;summary[9]=entries;summary[10]=order.size();
        summary[11]=persistent_halt;summary[12]=halt_days;summary[13]=max_close_dd;summary[14]=data_complete;summary[15]=valuation_valid;
        if(!std::isfinite(summary[0])||std::abs(summary[0]-100000.-net)>1e-6)return -3;
        return events_count;
    }catch(const CapacityFailure&){return -2;}catch(...){return -4;}
}
